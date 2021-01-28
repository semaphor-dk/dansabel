#!/usr/bin/env python3
import ruamel.yaml # YAML parser used in Ansible
import sys
import jinja2
import jinja2.sandbox
import os
import difflib # used for misspelled keyword suggestions
import textwrap
import argparse

verbosity = 0

 # set to False to return success when there is no parser error,
 # but jinjalint had comments; this should be a cli switch:
FAIL_WHEN_ONLY_ANNOTATIONS = True

USE_COLORS = False

VERTICAL_PIPE = '┃'
HORIZONTAL_PIPE = '━'
UNICODE_DOT = '•'

try:
    assert os.isatty(sys.stdout.fileno())
    OUT_COLS = os.get_terminal_size().columns
    OUT_ROWS = os.get_terminal_size().lines
    USE_COLORS = True
except: # It's not going to be pretty, but OK:
    OUT_COLS = 72
    OUT_ROWS = 25

import collections
class Colored(collections.UserString):
    def join(self, lst):
        res = Colored()
        is_first = True
        for x in lst:
            if not is_first:
                res += self
            is_first = False
            res += x
        return res
    def __init__(self, data=r'', colors=[], strs=[]):
        self.strs = []
        if isinstance(colors, str):
            colors = [colors]
        if strs or isinstance(data, Colored):
            self.strs = strs or data.strs
            self.data = ''.join(self.strs)
            self.colors = colors or data.colors
        elif isinstance(data, tuple):
            self.strs = list(map(str, data))
            self.data = ''.join(self.strs)
            self.colors = colors
        else:
            self.data = str(data)
            self.strs = [self.data]
            self.colors = colors
        # pad with last color:
        if len(self.colors) < len(self.strs):
            self.colors.extend( [self.colors[-1:] or r'RESET'] * (len(self.strs)-len(self.colors)) )

    def __add__(self, b):
        if isinstance(b, int):
            b = str(b)
        if isinstance(b, tuple):
            return self + Colored(b)
        elif isinstance(b, Colored):
            strs = self.strs + b.strs
            colors = self.colors + b.colors
            colors = colors[:len(strs)]
            res = Colored(None, colors=colors, strs=strs)
            return res
        elif isinstance(b, str):
            strs = self.strs + [b]
            colors = self.colors + ['RESET']
            assert len(colors) == len(strs)
            return Colored(None, strs=strs, colors=colors)
        raise Exception('unhandled', str(b))

def __vt100_color(tag, text):
    '''Wrap (text) in VT100 escape codes coloring according to (tag). Uses the xterm-256 color palette.'''
    RESET_COLOR = '\x1b[39;49;0m'
    prefix = RESET_COLOR
    if 'data' == tag: prefix= '\x1b[38:5:248:0m' # gray
    elif 'variable_begin' == tag or 'variable_end' == tag: prefix = '\x1b[38:5:91;1m' # purple
    elif 'operator' == tag: prefix = '\x1b[36;1m' # green
    elif 'block_begin' == tag or \
       'block_end' == tag: prefix = '\x1b[38:5:208;1m' # orange
    elif 'LEX_ERROR' == tag: prefix = '\x1b[38:5:217;1;41m'
    elif 'BOLD' == tag: prefix = '\x1b[1m'
    elif 'comment_begin' == tag or \
         'comment' == tag or \
         'comment_end' == tag: prefix = '\x1b[38:5:165m' # magenta/pink
    elif 'integer' == tag: prefix = '\x1b[38:5:108;1m' # white fg green bg
    elif 'name' == tag: prefix = '\x1b[38:5:10:20;1m' # green (no bg)
    elif 'string' == tag: prefix = '\x1b[38:5:197:0;1m' # red-ish
    elif 'whitespace' == tag or \
       'RESET' == tag: prefix = RESET_COLOR
    elif 'ERROR' == tag: prefix ='\x1b[38:5:15;1;41m' # white fg red bg
    elif 'NOT_CONSUMED' == tag:
        prefix = '\x1b[37;1;41m' # white fg red bg for the first two characters
        prefix += text[:2] + color_text('data', text[2:])
        text = ''
    else:
        output('\nBUG: please report this! unknown jinja2 lexer tag', tag)
        sys.exit(1)
    return f'{RESET_COLOR}{prefix}{text}{RESET_COLOR}{RESET_COLOR}'

def color_text(tag, text):
    '''Color (text) according to (text). Wobbles the indenting slightly when not using colors,
    but it should be legible.'''
    if USE_COLORS: return Colored(__vt100_color(tag, text))
    if 'NOT_CONSUMED' == tag: return f' -=NOT CONSUMED=- {repr(text)}'
    if 'ERROR' == tag: return f'e {text}'
    if 'RESET' == tag: return text
    return f'{text}'

def output(*args, sep=' ', **kwargs):
    '''print wrapper that may be redirected in a later iteration of this tool.'''
    is_first = True
    for element in args:
        if is_first:
            is_first = False
        else:
            output(sep ,end='')
        if isinstance(element, Colored):
            for text, color in zip(element.strs, element.colors):
                print(color_text(color, text), end='', sep='')
        else:
            print(element, end='')
    print('', end=kwargs.get('end', '\n'))

class Target(str):
    '''dummy class to let us keep a .node property'''

def lexed_loc(item):
    fst = item['lines'][0]
    lst = item['lines'][-1]
    if fst == lst:
        return f"line {fst['line']}:{fst['byteoff']}"
    elif fst['line'] == lst['line']:
        return f"line {fst['line']}:{fst['byteoff']}-{lst['byteoff']}"
    else:
        return f"lines {fst['line']}-{lst['line']}"

def token_text(item):
    return ''.join([x['text'] for x in item['lines']])

def tokens_match(left, right):
    # try to makes sure we match e.g '{%' and '-%}\n' with each other:
    left = left.rstrip('-').strip()
    right = right.lstrip('-').strip()
    return (left,right) in [ ('(',')'), ('[',']'), ('{','}'),
                             ('{{','}}'), ('{%','%}'), ('{#','#}'), ]

def is_scope_open(tok):
    if tok['tag'].endswith('_begin'): return True
    return ('operator' == tok['tag'] and token_text(tok) in ['[', '(', '{'])

def is_scope_close(tok):
    if tok['tag'].endswith('_end'): return True
    return ('operator' == tok['tag'] and token_text(tok) in [']', ')', '}'])

def print_lexed_debug(lexed, node_path, parse_e, lexer_e=None, annotations=[], debug=False):
    if all(map(lambda x: 'data' == x['tag'], lexed)): return
    if not isinstance(parse_e, Exception): # Target, not Exception (we always print parser exceptions)
        if not annotations: # skip when there are no parser exceptions and no annotations
            if not verbosity:
                return
    LAST_THRESHOLD = 3 # must be >=1
    relevant_lines = set()

    # first we try to establish which lines we are interested in looking at:
    marked_lines = set()
    if parse_e and parse_e.lineno: marked_lines.add(parse_e.lineno)
    if lexer_e and lexer_e.lineno: marked_lines.add(lexer_e.lineno)
    for annot in annotations:
        # go out on a limb and assume annotations will exist in (lexed)
        # find all annotations for this token
        for lin in annot['tok']['lines']:
            marked_lines.add(lin['line'])
            # if the helpful message is referring to another line,
            # ensure we also display that:
            for related_tok in annot['related_tokens']:
                for rel_line in related_tok['lines']:
                    marked_lines.add(rel_line['line'])
    for lineno in marked_lines:
        relevant_lines.update(range(lineno - LAST_THRESHOLD,
                                    lineno + LAST_THRESHOLD + 1))
    for line in relevant_lines.copy(): # copy because we update it:
        # remove one-line gaps; just print the line instead of "skipped 1 line":
        if line -2 in relevant_lines:
            relevant_lines.add(line-1)
    if verbosity:
        relevant_lines.update(range(0, lexed[-1]['lines'][-1]['line']+2))

    open_tag_stack = []
    current_line = 0
    linebuf = Colored('') # buffers one line of output
    last_printed = 0
    for tok in lexed:
        if is_scope_open(tok):
            open_tag_stack.append(tok['tag'])
        indent_level = len(open_tag_stack)
        offset = 14 + 2*(indent_level)
        if is_scope_close(tok):
            open_tag_stack = open_tag_stack[:-1] # .pop() without exception
        for lin in tok['lines']:
            is_new_line = (lin['line'] != current_line)
            if is_new_line:
                if current_line in relevant_lines:
                    skipped = current_line + min(-1-last_printed, -lexed[0]['lines'][0]['line'])
                    if skipped > 0: # for first line will be -1
                        if debug: output() # blank line for the debug view
                        output((UNICODE_DOT * 3).rjust(11) + ' (' + str(skipped)+ ' lines)',
                               end=(not debug and '\n' or ''))
                    last_printed = current_line
                    output(linebuf, end='')
                linebuf = Colored('')
                current_line = lin['line']

            line_color = 'data'
            if tok in map(lambda atok: atok['tok'], annotations):
                # TODO use a set instead, this is slow
                line_color = 'comment'
            if lexer_e and current_line == lexer_e.lineno: line_color = 'LEX_ERROR'
            if parse_e and current_line == parse_e.lineno: line_color = 'ERROR'

            if not debug: # this is the inline display:
                if is_new_line:
                    linebuf += Colored(str(current_line).ljust(5), line_color)
                linebuf += Colored(lin.get('text'), tok['tag'])
                if 'NOT_CONSUMED' == tok['tag']:
                    break # only print the first unlexed line
                continue

            transformed = repr(lin['text'])[1:-1] # strip single-quotes that repr() always adds
            transformed = transformed.replace("\\n", '↵')
            if ( abs(last_printed - current_line) <= 1) and tok['tag'] in ('whitespace', 'data'):
                # Tack insignificant tokens onto the end of the previous token display.
                # We don't do this for the first line, or after skipping.
                linebuf += Colored(' ' + transformed + ' ', tok['tag'])
                continue
            else:
                linebuf += Colored('\n')
            linebuf += Colored(str(lin['line']).rjust(4) + ':'+
                               str(lin['byteoff']).ljust(3), line_color)
            for color_tag in open_tag_stack[:-1]:
                linebuf += Colored(VERTICAL_PIPE +' ', color_tag)
            if is_scope_close(tok):
                if open_tag_stack:
                    linebuf += Colored(VERTICAL_PIPE +' ', open_tag_stack[-1])
                linebuf += Colored('┗' + HORIZONTAL_PIPE, tok['tag'])
            elif is_scope_open(tok):
                linebuf += Colored('┏' + HORIZONTAL_PIPE, tok['tag'])
            else:
                tag = open_tag_stack and open_tag_stack[-1] or tok['tag']
                linebuf += Colored('┣' + HORIZONTAL_PIPE, tag)
            linebuf += Colored(HORIZONTAL_PIPE * (indent_level), tok['tag'])
            linebuf += Colored(HORIZONTAL_PIPE * (offset-len(tok['tag'])), tok['tag'])+' '
            linebuf += tok['tag'] + ': '
            linebuf += Colored(transformed, tok['tag'])
            for annot in filter(lambda x: x['tok'] == tok, annotations):
                for msg in textwrap.wrap('\u269e ' + annot['comment'] + '\u269f', width=max(8, OUT_COLS - offset)):
                    linebuf += '\n' + ' '  * offset
                    linebuf += Colored(msg, 'comment')
            if 'NOT_CONSUMED' == tok['tag']: break # only print the first unlexed line
    if current_line in relevant_lines:
        output(linebuf, end='')
    if debug: # display with syntax highlighting inline
        output(Colored('\n' + HORIZONTAL_PIPE * OUT_COLS, 'string'))
        if verbosity:
            output(f'{UNICODE_DOT} {node_path}')
        if parse_e:
            output(f'{UNICODE_DOT} {node_path}', parse_e.lineno, Colored('jinja parser', 'ERROR'),
                   Colored(parse_e.message, 'ERROR'), sep=f' {VERTICAL_PIPE} ')
        if lexer_e:
            output(f'{UNICODE_DOT} {node_path}', lexer_e.lineno, Colored('jinja lexer', 'LEX_ERROR'),
                   Colored(lexer_e.message, 'LEX_ERROR'), sep=f' {VERTICAL_PIPE} ')
        output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))

JINJA_BUILTIN_FILTERS = set([
 'abs', 'float', 'lower', 'round', 'tojson', 'attr', 'forceescape', 'map', 'safe', 'trim', 'batch', 'format', 'max', 'select', 'truncate', 'capitalize', 'groupby', 'min', 'selectattr', 'unique', 'center', 'indent', 'pprint', 'slice', 'upper', 'default', 'int', 'random', 'sort', 'urlencode', 'dictsort', 'join', 'reject', 'string', 'urlize', 'escape', 'last', 'rejectattr', 'striptags', 'wordcount', 'filesizeformat', 'length', 'replace', 'sum', 'wordwrap', 'first', 'list', 'reverse', 'title', 'xmlattr', ])

# TODO should keep track of required/positional/keyword args and ansible versions these exist in:
ANSIBLE_BUILTIN_FILTERS = set(sum({
    # introduced in <= ansible v2.3:
    '20300': [
        'mandatory', 'ternary', 'bool', 'lookup', 'combine', 'map', 'permutations', 'product',
        'combinations', 'random', 'shuffle', 'unique', 'union', 'intersect', 'difference',
        'symmetric_difference', 'log', 'pow', 'root', 'hash', 'checksum', 'password_hash',
        'comment', 'to_datetime', 'to_uuid', 'regex_escape', 'regex_search', 'regex_replace',
        'basename', 'win_basename', 'win_splitdrive', 'dirname', 'win_dirname', 'expanduser',
        'realpath', 'relpath', 'splitext', 'path_join', 'quote', 'b64decode', 'b64encode',
        'from_json', 'to_json', 'from_nice_json', 'to_nice_json', 'from_yaml', 'to_yaml',
        'from_nice_yaml', 'to_nice_yaml', 'type_debug', 'zip', 'zip_longest',
        'd', # d() is an alias for default(), not sure if that's from ansible or jinja
        'json_query', ],
    '20400': [ 'strftime', 'urlsplit', ],
    '20500': ['flatten',],
    # 20600: ['dict2items', 'random_mac', 'expandvars',],
    # 20700: ['items2dict', 'subelements'],
}.values(), []))

# TODO detect currently installed ansible version or provide a way to configure per-repo?
BUILTIN_FILTERS = JINJA_BUILTIN_FILTERS.union(ANSIBLE_BUILTIN_FILTERS)

def parse_lexed(lexed):
    begins = []
    recommendations = []
    for i in range(len(lexed)):
        tok = lexed[i]
        this_token_closed = None # ref to popped begins[-1] if any
        def recommend(comment, token=lexed[i], related=[]):
            recommendations.append({'tok': token, 'comment': comment, 'related_tokens': related})
        ## This looks for "filters", aka tag {name} following {operator "|"}:
        if is_scope_open(tok):
            begins.append(tok)
        elif is_scope_close(tok):
            this_token_closed = begins.pop() # TODO should pop last matching type; anything else is an error
            if not tokens_match(token_text(this_token_closed), token_text(tok)):
                recommend('Unclosed block?', related=[this_token_closed])
        if 'operator' == tok['tag'] and token_text(tok) == '|':
            for next in lexed[i + 1:]: # skipping whitespace, TODO comments?
                if next['tag'] in ('whitespace',): continue
                if 'name' == next['tag']:
                    if token_text(next) in BUILTIN_FILTERS: continue
                    suggest = ', '.join(difflib.get_close_matches(
                        token_text(next), BUILTIN_FILTERS, 2,cutoff=0.1))
                    recommendations.append({
                        'tok': next,
                        'related_tokens': [],
                        'comment': 'Not a builtin filter? Maybe: ' + suggest})
                break
        # BELOW: Heuristics that depend on look-ahead:
        if i+1 == len(lexed): continue
        if 'operator' == tok['tag'] and 'operator' == lexed[i+1]['tag'] and \
           lexed[i] not in begins:
            #recommend('Two operators in a row?')
            if '{' == token_text(lexed[i]) and begins:
                recommend('Did you forget to close this? Nested tags found.',
                          token=begins[-1]['lines'][0])
        elif 'operator' == tok['tag'] and '}' == token_text(tok):
            cand = list(filter(lambda x: token_text(x).startswith('{'), begins))
            if cand and not (this_token_closed and tokens_match(token_text(this_token_closed), token_text(tok))):
                recommend('Found single "}" operator at ' + lexed_loc(lexed[i]) + \
                          ', did you mean to close '+ repr(token_text(cand[0])) + \
                          ' at ' + lexed_loc(cand[0]) + '?',
                          related=[cand[0]] # mark for display
                          )
    if begins:
        # TODO only warn if there's no lexer error?
        #if not any(filter(lambda x: 'NOT_CONSUMED' == x['tag'] and '}' in token_text(x), lexed)):
        recommendations.insert(0,{'tok': begins[-1],
                                  'comment': 'This may be an unclosed block?',
                                  'related_tokens': [],
                                  })
    return recommendations


def check_str(yaml_node, pos_stack):
    '''returns True on error, False on success'''
    s = yaml_node.value
    node_path = ''
    parse_e = Target()
    parse_e.lineno = 0 # elsewhere we treat 'not lineno' as lack of information
    lexer_e = Target()
    lexer_e.lineno = 0 # defined here because we may to lift an exc out of its scope
    for i, p in enumerate(pos_stack):
        if p[2]: # skip intermediary AST nodes that we have no name for
            if i > 1: node_path += '.'
            node_path += str(p[2])
    try:
        d = jinja2.sandbox.ImmutableSandboxedEnvironment().parse(source=s, name=node_path, filename='JINJA_TODO_FILENAME_SEEMS_UNUSED')
        # TODO good place to return False if we don't care about non-parser errors
    except jinja2.TemplateSyntaxError as parse_e_exc:
        parse_e = parse_e_exc

    # OK! Gloves off! We are going to run it through the lexer to retrieve
    # more information and hopefully be able to be helpful.
    # Idea here is to line it up so (file_line + lex_line) is the actual
    # line in the file, and (lex_col) is the actual column in the file.
    # The lex_line variable represents our attempt to follow the lexer.

    file_line = yaml_node.start_mark.line
    if '\n' in s:
        file_line += 1
        lex_col = yaml_node.start_mark.column
    parse_e.lineno = file_line + parse_e.lineno
    consumed = 0

    lex_line = 1
    lex_col = yaml_node.start_mark.column + 1
    lexed = []
    try:
        for rawtok in jinja2.sandbox.ImmutableSandboxedEnvironment().lex(source=s):
            consumed += len(rawtok[2])
            token = { 'tag': rawtok[1], 'lines': [] }
            for lineno, text in enumerate(rawtok[2].splitlines(True)):
                token['lines'].append({'line': file_line + lex_line,
                                       'byteoff': lex_col,
                                       'text': text})
                if text.endswith('\n'):
                    lex_line += 1
                    lex_col = yaml_node.start_mark.column + 1
                else:
                    lex_col += len(text)
            lexed.append(token)
    except jinja2.exceptions.TemplateSyntaxError as lex_e_exc:
        if str(parse_e) != str(lex_e_exc): # ignore redundant msgs
            lexer_e = lex_e_exc
            lexer_e.lineno = lexer_e.lineno + file_line
    if (consumed + 1 == len(s)) and '\n' == s[-1]:
        pass # ignore these trailing newlines
    elif consumed < len(s):
        not_consumed = {'tag': 'NOT_CONSUMED', 'lines': []}
        for lin in s[consumed:].splitlines(True):
            not_consumed['lines'].append({'line': file_line + lex_line,
                                          'byteoff': lex_col,
                                          'text': lin})
            lex_line += 1
            lex_col += len(lin)
        lexed.append(not_consumed)
    annotations = parse_lexed(lexed)
    print_lexed_debug(lexed, node_path, parse_e, lexer_e, annotations=annotations,
                      debug=False)
    if annotations or (verbosity and len(lexed)>1):
        output('\n' + '~' * OUT_COLS) # separate the inline view from per-token listing
        print_lexed_debug(lexed, node_path, parse_e, lexer_e,
                          annotations=annotations, debug=True)
        return FAIL_WHEN_ONLY_ANNOTATIONS
    return isinstance(parse_e, Exception)

S_KEY = 10
S_VAL = 20
S_SEQ = 30

def check_val(doc, pos_stack, error=False):
    state = [ (S_VAL,0) ] # list of tuples of state and data (used for list item counting)
    while True:
        try:
            v = next(doc)
        except StopIteration as e:
            output(Colored('\n' + HORIZONTAL_PIPE * OUT_COLS, 'ERROR'))
            output(str(e))
            output('YAML parser/lexer exit before end of document.')
            output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'ERROR'))
            return True # this is an error
        # TODO need to implement special handling of the 'when:' keys
        if isinstance(v, ruamel.yaml.events.ScalarEvent):
            if S_KEY == state[-1][0]:
                error |= check_str(v, pos_stack)
                state[-1] = (S_VAL, None)
                if 'when' == v.value:
                    state[-1] = (S_VAL, 'when') # need to implement special handling
                if 'name' == v.value:
                    state[-1] = (S_VAL, 'name') # TODO need to handle
                # here we change the name of the parent mapping itself (starts out as empty):
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], v.value)
            elif S_SEQ == state[-1][0]:
                error |= check_str(v, pos_stack)
                next_idx = state[-1][1] + 1
                state[-1] = (state[-1][0], next_idx)
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], next_idx)
            elif S_VAL == state[-1][0]:
                if state[-1][1] == 'name':
                    error |= check_str(v, pos_stack)
                    # set context name of the parent node to the value of this:
                    if len(state) > 1 and state[-2][0] == S_SEQ:
                        pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], v.value)
                elif state[-1][1] == 'when':
                    # TODO this is kind of a hack, and it skews the column numbers:
                    v.value = '{{' + v.value + '}}'
                    error |= check_str(v, pos_stack)
                else:
                    error |= check_str(v, pos_stack)
                state[-1] = (S_KEY, None)
        elif isinstance(v, ruamel.yaml.events.SequenceStartEvent) \
             or isinstance(v, ruamel.yaml.events.MappingStartEvent):
            # Usually when we reach here we will be in either S_SEQ (a list item)
            # or S_VAL state. If we are in S_VAL state, we need to transition
            # to S_KEY state in the parent context (because this mapping will be
            # said mapping):
            if state and state[-1][0] == S_VAL:
                state[-1] = (S_KEY, state[-1][1])
            elif state and state[-1][0] == S_SEQ:
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], str(state[-1][1]))
                next_idx = state[-1][1] + 1
                state[-1] = (state[-1][0], next_idx)
            # Open a new context for the contents of this mapping:
            if isinstance(v, ruamel.yaml.events.SequenceStartEvent):
                state.append( (S_SEQ, 0) )
                pos_stack.append((v.start_mark, v.end_mark, 'SEQ'))
            else:
                state.append((S_KEY, None))
                pos_stack.append((v.start_mark, v.end_mark, 'MAP'))
        elif isinstance(v, ruamel.yaml.events.MappingEndEvent):
            state.pop()
            pos_stack.pop()
        elif isinstance(v, ruamel.yaml.events.SequenceEndEvent):
            old = state.pop()
            assert(old[0] == S_SEQ)
            pos_stack.pop()
        elif isinstance(v, ruamel.yaml.events.DocumentStartEvent): pass
        elif isinstance(v, ruamel.yaml.events.DocumentEndEvent):   pass
        elif isinstance(v, ruamel.yaml.events.StreamStartEvent):   pass
        elif isinstance(v, ruamel.yaml.events.StreamEndEvent):
            break
        else:
            output(pos_stack, f'\nBUG: please report this! unhandled YAML type {repr(v)}') #, file=os.stderr)
            error = True
    return error

def raw_scalar_generator(payload):
    '''Mock parse event generator for raw jinja2 files'''
    yield ruamel.yaml.events.StreamStartEvent()
    yield ruamel.yaml.events.DocumentStartEvent()
    yield ruamel.yaml.events.MappingStartEvent(anchor=None, tag=None, implicit=True, flow_style=False)
    start_mark = ruamel.yaml.reader.FileMark(payload, index=0, column=-1, line=-1)
    with open(payload) as fd:
        yield ruamel.yaml.events.ScalarEvent(anchor=None, tag=None, implicit=(True, False), value=fd.read(), style='',start_mark=start_mark)
    # signal to our own parser that we completed successfully:
    yield ruamel.yaml.events.DocumentEndEvent()
    yield ruamel.yaml.events.StreamEndEvent()

def ruamel_generator(filename):
    try:
        with open(filename) as fd:
            for z in ruamel.yaml.YAML(typ=r'safe').parse(fd):
                yield z
    # All exception handlers here must output a description, or at the very least
    # a backtrace:
    except ruamel.yaml.scanner.ScannerError as e:
        err = str(e)
        if 'while scanning a simple key' == e.context:
            if "could not find expected ':'" == e.problem:
                err += f'\nThe dictionary entry{e.context_mark} appears to lack indenting.'
        return err
    except ruamel.yaml.parser.ParserError as e:
        err = str(e)
        if 'while parsing a block mapping' == e.context and 'did not find expected key' == e.problem:
            if e.context_mark.column == e.problem_mark.column:
                err += '\nThe following line must not have the same indent.'
            elif e.context_mark.column > e.problem_mark.column:
                err += '\nEither the line needs indentation or the key is missing?'
        # here we could look for next line that doesn't start with whitespace and restart
        # the parser?
        return err # this will raise a StopIteration exception in the consumer

def lint(filename):
    try:
        if filename.endswith('.yml'):
            doc = ruamel_generator(filename)
        else: # assume it's raw jinja2, mock up AST nodes:
            doc = raw_scalar_generator(filename)
        return check_val(doc, pos_stack=[(0,0,filename + ':')])
    except Exception as e:
        import traceback
        output(traceback.format_exc())
        return True # that did not go well, perhaps file not found or yaml parsing err

if '__main__' == __name__:
    a_parser = argparse.ArgumentParser()
    a_parser.add_argument('FILE', nargs='+')
    a_parser.add_argument('-C', '--context-lines', type=int, help="NOT IMPLEMENT Number of context lines controls LAST_THRESHOLD")
    args = a_parser.parse_args()

    error = False
    # we do not support any flags, so for now we just:
    for filename in args.FILE:
        if '--' == filename: continue
        error |= lint(filename)
    sys.exit(error)
