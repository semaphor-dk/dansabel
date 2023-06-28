#!/usr/bin/env python3
import ruamel.yaml # YAML parser used in Ansible
import sys
import jinja2
import jinja2.sandbox
import os
import difflib # used for misspelled keyword suggestions
import textwrap
import argparse
import shlex
import json
import importlib
import ansible.plugins.filter
import ansible.plugins.test
import pkgutil
try:
    import ansible_collections.community.general.plugins.filter
    SKIP_COMMUNITY = False
except:
    SKIP_COMMUNITY = True

# from ansible_collections.ansible_release import ansible_version
# ^- retrieve the ansible version we are checking against

verbosity = 0
LAST_THRESHOLD = 3 # must be >=1

 # set to False to return success when there is no parser error,
 # but jinjalint had comments; this should be a cli switch:
FAIL_WHEN_ONLY_ANNOTATIONS = True

USE_COLORS = False

EXTERNAL_VARIABLES = dict()
SEEN_TAGS = dict() # filename -> tags: conditionals
ANCHORS = dict() # aliases/anchors defined
ALIASED_ANCHORS = dict() # aliases/anchor used (referring to ANCHORS)

VERTICAL_PIPE = '┃'
HORIZONTAL_PIPE = '━'
UNICODE_DOT = '•'

try:
    assert os.isatty(sys.stdout.fileno())
    OUT_COLS = os.get_terminal_size().columns or 72 # it's 0 for ptys
    USE_COLORS = True
except: # It's not going to be pretty, but OK:
    OUT_COLS = 72


# we will use the same immutable jinja environment instead of instantiating a new one
# for each string field in each YAML file, for better performance.
JINJA2_SANDBOX_ENVIRON = jinja2.sandbox.ImmutableSandboxedEnvironment()

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
    elif tag in (
            'block_begin',
            'block_end',
            'raw_begin',
            'raw_end'): prefix = '\x1b[38:5:208;1m' # orange
    elif 'LEX_ERROR' == tag: prefix = '\x1b[38:5:217;1;41m'
    elif 'BOLD' == tag: prefix = '\x1b[1m'
    elif 'comment_begin' == tag or \
         'comment' == tag or \
         'comment_end' == tag: prefix = '\x1b[38:5:165m' # magenta/pink
    elif tag in ('integer','IF'): prefix = '\x1b[38:5:108;1m' # white fg green bg
    elif tag in ('name', 'FOR'): prefix = '\x1b[38:5:10:20;1m' # green (no bg)
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
                             ('{{','}}'), ('{%','%}'), ('{#','#}'),
                             ('{% raw %}', '{% endraw %}'), ]

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
    next_scope_transition = []
    current_line = 0
    linebuf = Colored('') # buffers one line of output
    last_printed = 0
    for idx, tok in enumerate(lexed):
        if is_scope_open(tok):
            nextnwsp = first_non_whitespace(lexed[idx+1:])
            if nextnwsp and nextnwsp['tag'] == 'name':
                if token_text(nextnwsp) in ('if',): # elif stays in same scope
                    next_scope_transition.append((len(open_tag_stack),'IF'))
                elif token_text(nextnwsp) in ('for',):
                    next_scope_transition.append((len(open_tag_stack),'FOR'))
                elif open_tag_stack:
                    if (token_text(nextnwsp) == 'endfor' and open_tag_stack[-1]=='FOR') or (
                            token_text(nextnwsp) == 'endif' and open_tag_stack[-1]=='IF'):
                        open_tag_stack = open_tag_stack[:-1]
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
        # we're still looping over tokens, here we effectuate the changed scope when leaving a block:
        if idx-1 >= 0 and is_scope_close(first_non_whitespace(lexed[idx-1::-1])):
            for nst_idx, (scope_len, typ) in enumerate(next_scope_transition):
                if len(open_tag_stack) == scope_len:
                    open_tag_stack.append(typ)
                    del next_scope_transition[nst_idx]

    if current_line in relevant_lines:
        output(linebuf, end='')
    if debug: # display with syntax highlighting inline
        output(Colored('\n' + HORIZONTAL_PIPE * OUT_COLS, 'string'))
        if parse_e or lexer_e:
            if parse_e:
                output(f'{UNICODE_DOT} {node_path}', parse_e.lineno, Colored('jinja parser', 'ERROR'),
                    Colored(parse_e.message, 'ERROR'), sep=f' {VERTICAL_PIPE} ')
            if lexer_e:
                output(f'{UNICODE_DOT} {node_path}', lexer_e.lineno, Colored('jinja lexer', 'LEX_ERROR'),
                    Colored(lexer_e.message, 'LEX_ERROR'), sep=f' {VERTICAL_PIPE} ')
        else:
            output(f'{UNICODE_DOT} {node_path}')
        output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))
    elif verbosity == 1:
        # TODO == 1 prevents the double printing when verbosity>=2; this could be prettier.
        output(Colored('\n' + HORIZONTAL_PIPE * OUT_COLS, 'string'))
        output(f'{UNICODE_DOT} {node_path}')
        output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))


def load_ansible_collections_filters():
    import importlib
    from pathlib import Path
    import ansible_collections
    for f in Path(ansible_collections.__path__[0]).glob('**/plugins/filter/*.py'):
        if f.stem.startswith('_'): continue
        parts = f._parts[f._parts.index('ansible_collections') :]
        parts[-1] = parts[-1].replace('.py', '')
        mod = importlib.import_module('.'.join(parts), package=ansible_collections)
        filters = mod.FilterModule().filters().keys()
        filter_ns = parts[1:-3] # skip 'ansible_collections/' and 'plugins/filters'
        for fname in filters:
            yield fname
            yield '.'.join(parts[1:-3] + [fname])

# "XXX is YYY(...)" where YYY is a test and ... is zero or more arguments:
# https://jinja.palletsprojects.com/en/3.0.x/templates/#builtin-tests
JINJA_BUILTIN_TESTS = set(jinja2.tests.TESTS)

# https://docs.ansible.com/ansible/latest/user_guide/playbooks_tests.html
# https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/test/core.py#L235
ANSIBLE_BUILTIN_TESTS = set().union(*[
    set(importlib.import_module('ansible.plugins.test.' + name).TestModule().tests())
    for loader, name, is_pkg in pkgutil.walk_packages(ansible.plugins.test.__path__)
])

JINJA_BUILTIN_FILTERS = set(jinja2.filters.FILTERS)

ANSIBLE_BUILTIN_FILTERS = set()
if not SKIP_COMMUNITY:
    ANSIBLE_BUILTIN_FILTERS = ANSIBLE_BUILTIN_FILTERS.union(
        load_ansible_collections_filters()
    )
ANSIBLE_BUILTIN_FILTERS.update(*[
    importlib.import_module('ansible.plugins.filter.' + name).FilterModule().filters()
    for loader, name, is_pkg in pkgutil.walk_packages(ansible.plugins.filter.__path__)
], {'lookup','query','now','undef'})
# https://github.com/ansible/ansible/blob/2058ea59915655d71bf5bd9d3f7e318ffec3c658/lib/ansible/template/__init__.py#L649-L653
# ^-- the hardcoded values above are currenty not accounted for.

# Here we find 'd', 'e', etc:
mock_template_env = ansible.template.AnsibleEnvironment()
ANSIBLE_BUILTIN_FILTERS.update(mock_template_env.filters)
ANSIBLE_BUILTIN_TESTS.update(set(mock_template_env.tests))

BUILTIN_TESTS = JINJA_BUILTIN_TESTS.union(ANSIBLE_BUILTIN_TESTS)
BUILTIN_FILTERS = JINJA_BUILTIN_FILTERS.union(ANSIBLE_BUILTIN_FILTERS)

def first_non_whitespace(tok_list):
    for tok in tok_list:
        if tok['tag'] in (r'whitespace',): continue
        return tok

def parse_lexed(lexed):
    begins = []
    recommendations = []
    for i in range(len(lexed)):
        tok = lexed[i]
        tok_text = token_text(tok)
        this_token_closed = None # ref to popped begins[-1] if any
        def recommend(comment, token=lexed[i], related=[]):
            recommendations.append({'tok': token, 'comment': comment, 'related_tokens': related})
        ## This looks for "filters", aka tag {name} following {operator "|"}:
        if is_scope_open(tok):
            if tok['tag'] == 'block_begin':
                next = first_non_whitespace(lexed[i+1:])
                if next:
                    next_text = token_text(next)
                    if next_text == 'if':
                        begins.append(next)
                    elif next_text == 'for':
                        begins.append(next)
                    elif next_text == 'elif':
                        popped = begins.pop()
                        if token_text(popped) not in ('elif'):
                            recommend(f'elif must not end a "{token_text(popped)}" scope', token=next, related=[popped])
                        begins.append(next)
                    elif next_text in ('endif', 'endfor'):
                        # we have endif/endfor, ensure they close the right scope:
                        popped = None
                        try:
                            popped = begins.pop() # TODO should be a 'endif'
                        except IndexError:
                            recommend(f'block closure, but no block scope is open',
                                      token=next, related=[tok])
                        if popped:
                            if (('endfor' == next_text and token_text(popped) not in ('for',)) or
                                ('endif'  == next_text and token_text(popped) not in ('if','elif'))):
                                recommend(f'{next_text} cannot not end a "{token_text(popped)}" scope', token=next, related=[popped])
            begins.append(tok)
        elif is_scope_close(tok):
            this_token_closed = begins.pop() # TODO should pop last matching type; anything else is an error
            if not tokens_match(token_text(this_token_closed), tok_text):
                recommend('Unclosed block?', related=[this_token_closed])
        if 'operator' == tok['tag'] and tok_text == '|':
            # We expect a filter to follow. Filters are either 'name'
            # or they are 'name' 'operator .' 'name', ...
            next_lexed = lexed[i + 1:]
            for next_idx, next in enumerate(next_lexed):
                # skipping whitespace, TODO comments?
                if next['tag'] in ('whitespace',): continue
                if 'operator' == next['tag']:
                    if '|' == token_text(next): # "||" results in two operators '|','|':
                        recommendations.append({'tok':next, 'related_tokens': [tok],
                                                'comment': 'Did you mean "or" ?',})
                        break
                elif 'name' == next['tag']:
                    tag_suffix = []
                    while (len(next_lexed) - next_idx > 0
                        and (next_lexed[next_idx+1]['tag'] == 'operator'
                             and token_text(next_lexed[next_idx+1]) == '.'
                             )):
                        next_idx += 2
                        if next_lexed[next_idx]['tag'] == 'name':
                            tag_suffix.append(token_text(next_lexed[next_idx]))
                        else:
                            break
                    this_text = token_text(next)
                    if tag_suffix:
                        this_text = joined = '.'.join([this_text, *tag_suffix])
                    if this_text in BUILTIN_FILTERS: break
                    suggest = ', '.join(difflib.get_close_matches(
                        this_text, BUILTIN_FILTERS, 2,cutoff=0.1))
                    recommendations.append({
                        'tok': next,
                        'related_tokens': [],
                        'comment': 'Not a builtin filter? Maybe: ' + suggest})
                break
        elif 'NOT_CONSUMED' == tok['tag']:
            if tok_text.startswith('&&'):
                recommendations.append({'tok':tok, 'related_tokens': [],
                                        'comment': 'Did you mean "and" ?',
                                        })
        # BELOW: Heuristics that depend on look-ahead:
        if i+1 == len(lexed): continue
        if 'operator' == tok['tag'] and 'operator' == lexed[i+1]['tag'] and \
           lexed[i] not in begins:
            #recommend('Two operators in a row?')
            if '{' == token_text(lexed[i]) and begins:
                recommend('Did you forget to close this? Nested tags found.',
                          token=begins[-1]['lines'][0])
        elif 'operator' == tok['tag'] and '}' == tok_text:
            cand = list(filter(lambda x: token_text(x).startswith('{'), begins))
            if cand and not (this_token_closed and tokens_match(token_text(this_token_closed), tok_text)):
                recommend('Found single "}" operator at ' + lexed_loc(lexed[i]) + \
                          ', did you mean to close '+ repr(token_text(cand[0])) + \
                          ' at ' + lexed_loc(cand[0]) + '?',
                          related=[cand[0]] # mark for display
                          )
        elif 'name' == tok['tag'] and tok_text in ('is', 'ansible_distribution'):
            next_i = -1
            for next in lexed[i+1:]:
                if next['tag'] in ('whitespace',): continue
                next_i += 1 # next_i is like enumerate(lexed), but skipping whitespace
                if 'is' == tok_text:
                    if next_i == 0 and token_text(next) == 'not': continue
                    if token_text(next) in BUILTIN_TESTS: break
                    suggest = ', '.join(difflib.get_close_matches(
                        token_text(next), BUILTIN_TESTS, 2,cutoff=0.1))
                    recommendations.append({'tok': next,
                                            'related_tokens': [tok],
                                            'comment': 'Not a builtin Test? Maybe: ' + suggest})
                    break
                elif 'ansible_distribution' == tok_text:
                    # Fix for issues/8 ; spell-checking
                    # (name:ansible_distribution)
                    # (operator tokens) / (whitespace tokens)
                    # (string:next)
                    if next['tag'] in ('operator',): continue
                    if 'string' == next['tag']:
                        distro = token_text(next).strip(r'"\'')
                        # source:
                        # https://docs.ansible.com/ansible/latest/user_guide/playbooks_conditionals.html#ansible-facts-distribution
                        suggests = difflib.get_close_matches(
                            distro,
                            ('Alpine','Altlinux','Amazon','Archlinux','ClearLinux','Coreos',
                             'CentOS','Debian','Fedora','Gentoo','Mandriva','NA','OpenWrt',
                             'OracleLinux','RedHat','Slackware','SLES','SMGL','SUSE','Ubuntu',
                             'VMwareESX', # extras:
                             'Kali','OpenSUSE','FreeBSD','Red Hat Enterprise Linux'),
                            cutoff=0.25)
                        if distro not in suggests:
                            recommendations.append({
                                'tok': next, 'related_tokens':[tok],
                                'comment': f'Did you mean {suggests} ?',})
                    break
                else:
                    break # break the "for next in ..." if we're never going to match anything.

    if begins:
        # TODO only warn if there's no lexer error?
        #if not any(filter(lambda x: 'NOT_CONSUMED' == x['tag'] and '}' in token_text(x), lexed)):
        recommendations.insert(0,{'tok': begins[-1],
                                  'comment': 'This may be an unclosed block?',
                                  'related_tokens': [],
                                  })
    return recommendations

def get_node_path(pos_stack):
    '''Returns a string representation of the AST node's path'''
    node_path = ''
    for i, p in enumerate(pos_stack):
        if p[2]: # skip intermediary AST nodes that we have no name for
            if i > 1: node_path += '.'
            node_path += str(p[2])
    return node_path

def check_str(yaml_node, pos_stack):
    '''returns True on error, False on success'''
    s = yaml_node.value
    parse_e = Target()
    parse_e.lineno = 0 # elsewhere we treat 'not lineno' as lack of information
    lexer_e = Target()
    lexer_e.lineno = 0 # defined here because we may to lift an exc out of its scope
    node_path = get_node_path(pos_stack)

    # TODO: '>' is "folded" style, where newlines are supposed to be replaced by spaces.
    # in ruamel that means turning \x0a into \x07\x0a. I don't think Jinja2 cares,
    # for parsing purposes, whether whitespace is \n or \07, but if we keep the newlines
    # here, our locs will be correct; if we replace them with spaces we need to special
    # case that in the line tracker below to keep the correspondence between Jinja2 errors
    # and physical location. Thus our solution for now will be:
    if yaml_node.style == '>':
        s = s.replace('\x07', '')
    try:
        jinja_template = JINJA2_SANDBOX_ENVIRON.parse(source=s, name=node_path, filename='JINJA_TODO_FILENAME_SEEMS_UNUSED')
        # TODO good place to return False if we don't care about non-parser errors
    except jinja2.TemplateSyntaxError as parse_e_exc:
        parse_e = parse_e_exc
    else:
        # Parsing was successful. Here we do bookkeeping on variables needed / defined:
        parsed_symbols = jinja2.idtracking.symbols_for_node(jinja_template)
        for ref in parsed_symbols.loads.values():
            if 'resolve' == ref[0]:
                # ref[1] contains the variable name of a variable that jinja
                # would need to resolve from the environment.
                filename = pos_stack[0][2].rstrip(':')
                EXTERNAL_VARIABLES[filename] = EXTERNAL_VARIABLES.get(filename, set())
                EXTERNAL_VARIABLES[filename].add(ref[1])

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
        for rawtok in JINJA2_SANDBOX_ENVIRON.lex(source=s):
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
    if annotations or (verbosity>=2 and len(lexed)>1) or not isinstance(parse_e, Target):
        output('\n' + '~' * OUT_COLS) # separate the inline view from per-token listing
        print_lexed_debug(lexed, node_path, parse_e, lexer_e,
                          annotations=annotations, debug=True)
        return FAIL_WHEN_ONLY_ANNOTATIONS
    return isinstance(parse_e, Exception)

def check_shell_command(v, pos_stack):
    '''Best-effort shell parsing'''
    error = False
    text = v.value
    s = shlex.shlex(text, posix=True, punctuation_chars=True)
    s.whitespace_split = True
    try:
        cmd = shlex.split(text)
    except ValueError as e:
        output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))
        error = True
        cmd = None
        last_loc = (v.start_mark.line, 0)
        try:
            for tok in s:
                this_loc = v.start_mark.line + s.lineno, s.instream.tell()
                # print locations for each lexed token:
                #output(Colored(str(last_loc) + '-'+(str(this_loc))+':'+repr(tok), 'ERROR'), endline='')
                last_loc = this_loc
        except ValueError as e:
            lex_stop = s.instream.tell()
            www = text[:last_loc[1]].split('\n')
            output(Colored(text[:last_loc[1]], 'variable_begin'), Colored(text[last_loc[1]:lex_stop].rstrip(), 'ERROR'))
            output(Colored('SHELL PARSING ERROR', 'ERROR'), f'{get_node_path(pos_stack)}:', 'line', v.start_mark.line + s.lineno, Colored(e, 'ERROR'))

    # BELOW: Warn about things that bite:
    # Should really generalize the printing/annotation parts from the jinja parser with the shell parsing.
    context = f'in {get_node_path(pos_stack)} line:{v.start_mark.line+1}'
    if cmd and 'psql' in cmd:
        if not 'ON_ERROR_STOP=' in text:
            output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))
            output(Colored('psql command without -v ON_ERROR_STOP=1', 'comment'), f'{context} - if this SQL command fails, it will still exit with exit code status zero (success) and Ansible will not detect the error. Also consider --single-transaction if you do not explicitly use transactions.')
            error = True
    if ';}' in cmd or ';};' in cmd: # detects most common broken shell grouping
        output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'string'))
        output(Colored('WARNING: ";}" found, did you mean "; }" ?', 'comment'), context)

    # TODO: return error
    # Commented out for now because we risk failing perfectly fine commands
    # that are subject to Jinja2 expansion when the Jinja2 templating itself constitutes
    # a parsing error
    return False

S_KEY = 10
S_VAL = 20
S_SEQ = 30

def check_val(doc, pos_stack, error=False):
    state = [ (S_VAL, 0, set()) ]
    # list of tuples of state and data (used for list item counting). The set
    # keeps track of siblings keys to enable duplicate detection.
    while True:
        try:
            v = next(doc)
        except StopIteration as e:
            output(Colored('\n' + HORIZONTAL_PIPE * OUT_COLS, 'ERROR'))
            output(str(e))
            output('YAML parser/lexer exit before end of document.')
            output(Colored(HORIZONTAL_PIPE * OUT_COLS, 'ERROR'))
            return True # this is an error

        if getattr(v,'anchor',None) and not isinstance(v, ruamel.yaml.events.AliasEvent):
            # https://www.educative.io/blog/advanced-yaml-syntax-cheatsheet#anchors
            # similar to HTML <a id="v.anchor">
            ANCHORS[v.anchor] = v
        # TODO need to implement special handling of the 'when:' keys
        if isinstance(v, ruamel.yaml.events.ScalarEvent):
            if S_KEY == state[-1][0]:
                error |= check_str(v, pos_stack)
                state[-1] = (S_VAL, v.value, *state[-1][2:])
                # 'name', 'when', etc need special handling
                # here we change the name of the parent mapping itself (starts out as empty):
                state[-1][2].add(v.value)
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], v.value)
            elif S_SEQ == state[-1][0]:
                error |= check_str(v, pos_stack)
                if len(state) >= 2 and state[-2][0] == S_KEY and state[-2][1] == 'tags':
                    # tags: [ ..., v , ... ]: collect these for display
                    filename = pos_stack[0][2].rstrip(':')
                    SEEN_TAGS[filename] = SEEN_TAGS.get(filename, set())
                    SEEN_TAGS[filename].add(v.value)
                next_idx = state[-1][1] + 1
                state[-1] = (state[-1][0], next_idx)
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], next_idx)
            elif S_VAL == state[-1][0]:
                if state[-1][1] == 'tags':
                    # when it's a scalar value, it's split by comma
                    filename = pos_stack[0][2].rstrip(':')
                    SEEN_TAGS[filename] = SEEN_TAGS.get(filename, set())
                    SEEN_TAGS[filename].update(map(lambda x:x.strip(), v.value.split(',')))
                if state[-1][1] == 'name':
                    error |= check_str(v, pos_stack)
                    # set context name of the parent node to the value of this:
                    if len(state) > 1 and state[-2][0] == S_SEQ:
                        pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], v.value)
                elif state[-1][1] == 'when':
                    # TODO this is kind of a hack, and it skews the column numbers:
                    v.value = '{{' + v.value + '}}'
                    error |= check_str(v, pos_stack)
                elif state[-1][1] in (r'cmd', r'shell', r'ansible.builtin.shell'):
                    # Special casing for shell commands
                    # TODO technically speaking we should only do this within the 'shell' module, not the 'command' module.
                    error |= check_str(v, pos_stack)
                    # TODO should probably be careful about complaining about shell lexing errors if
                    # the string is subject to Jinja expansion.
                    error |= check_shell_command(v, pos_stack)
                else:
                    error |= check_str(v, pos_stack)
                state[-1] = (S_KEY, None, *state[-1][2:])
        elif isinstance(v, ruamel.yaml.events.SequenceStartEvent) \
             or isinstance(v, ruamel.yaml.events.MappingStartEvent):
            # Usually when we reach here we will be in either S_SEQ (a list item)
            # or S_VAL state. If we are in S_VAL state, we need to transition
            # to S_KEY state in the parent context (because this mapping will be
            # said mapping):
            if state and state[-1][0] == S_VAL:
                state[-1] = (S_KEY, *state[-1][1:])
            elif state and state[-1][0] == S_SEQ:
                pos_stack[-1] = (pos_stack[-1][0], pos_stack[-1][1], str(state[-1][1]))
                next_idx = state[-1][1] + 1
                state[-1] = (state[-1][0], next_idx)
            # Open a new context for the contents of this mapping:
            if isinstance(v, ruamel.yaml.events.SequenceStartEvent):
                # for sequence states we track the list item offset
                state.append( (S_SEQ, 0) )
                pos_stack.append((v.start_mark, v.end_mark, 'SEQ'))
            else:
                # for mappings we track immediate child keys in a set
                state.append((S_KEY, None, set()))
                pos_stack.append((v.start_mark, v.end_mark, 'MAP'))
        elif isinstance(v, ruamel.yaml.events.MappingEndEvent):
            error |= lint_ansible_directives(v, state, pos_stack)
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
        elif isinstance(v, ruamel.yaml.events.AliasEvent):
            # an AliasEvent is when something tries to include/refer to an "anchor",
            # similar to <a href="#anchor">
            if v.anchor:
                ALIASED_ANCHORS[v.anchor] = v
        else:
            output(pos_stack, f'\nBUG: please report this! unhandled YAML type {repr(v)}') #, file=sys.stderr)
            error = True
    return error

def lint_ansible_directives(v:ruamel.yaml.events.MappingEndEvent, state, pos_stack):
    '''Lints Ansible directives by looking at keys and values.'''
    filepath = pos_stack[0][2]
    if '.github/workflows/' in filepath: return False # skip github triggers
    if 'host_vars/' in filepath or 'group_vars/' in filepath or '/defaults/' in filepath:
        return False # no error, we're looking for tasks

    #### The rest of this function looks for cases where a task has more than one module:
    if state[-1][0] != S_KEY: return False
    sibling_keys = state[-1][2]
    if 'name' not in sibling_keys: return False
    for st in state[:-1]:
        # here we loop over our ancestors and stop at the first "name:"
        # the idea being that if they have a name:, we are probably not a task ourselves.
        if st[0] == S_KEY and 'name' in st[2]:
            if 'block' in st[2]:
                # the exception is named block:s, we descend into those
                continue
            return False # no error
    # TODO this list is probably not exhaustive:
    # TODO pull all the with_* from ansible/plugins/lookup/ etc
    ANSIBLE_EXPECTED_DUPLS = {
        'with_config','with_csvfile','with_dict','with_env','with_fileglob','with_file',
        'with_first_found','with_indexed_items','with_ini','with_inventory_hostnames','with_items',
        'with_lines','with_list','with_nested','with_password','with_pipe','with_random_choice',
        'with_sequence','with_subelements','with_template','with_together','with_unvault','with_url',
        'with_varnames','with_vars',
        'name','hosts','notify','loop','name',
        'become', 'become_user','become_args',
        'ignore_errors','when','tags', 'register',
        'vars','args','loop_control',
        'environment','retries','run_once',
        'failed_when','changed_when','delegate_to', 'until', 'delay',
        'listen', # for handlers
        'roles','pre_tasks','gather_facts', 'connection', 'tasks', # TODO these are not actually valid inside tasks,
        # but listing them here lowers the number of false positives when accidentally
        # running jinjalint.py on a playbook.
        # (we SHOULD be able to handle playbooks, since we are a commit hook for *.yml)
    }
    diff = sibling_keys.difference(ANSIBLE_EXPECTED_DUPLS)
    if len(diff) > 1:
        output(Colored('WARNING: potentially conflicting modules:', 'raw_begin'), diff, f'at {get_node_path(pos_stack[:-1])} lines { pos_stack[-1][0].line}-{ v.end_mark.line }')
        return True
    return False

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
            yaml_obj = ruamel.yaml.YAML(typ=r'rt',pure=True)
            if ruamel.yaml.version_info[0:2] < (0,15):
                # backwards compatibility:
                yield from yaml_obj.parse(fd)
            else: # >=0.15 changed api:
                _, parser = yaml_obj.get_constructor_parser(fd)
                event = True
                while event:
                    event = parser.state()
                    yield event
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
        if filename.endswith('.yaml') or filename.endswith('.yml'):
            doc = ruamel_generator(filename)
        else: # assume it's raw jinja2, mock up AST nodes:
            doc = raw_scalar_generator(filename)
        return check_val(doc, pos_stack=[(0,0,filename + ':')])
    except Exception as e:
        import traceback
        output(traceback.format_exc())
        return True # that did not go well, perhaps file not found or yaml parsing err

if '__main__' == __name__:
    a_parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Lints each of the provided FILE(s) for jinja2/yaml errors.',
        epilog=
'''EXAMPLES

  List external variables used from Jinja:
  jinjalint.py -q --external ./*.j2 ./*.yml

  List tags encountered in YAML files:
  jinjalint.py -q --tags testcases/good/*.yml
''')
    a_parser.add_argument('FILE', nargs='+')
    a_parser.add_argument('-C', '--context-lines', type=int, help="Number of context lines controls LAST_THRESHOLD")
    a_parser.add_argument('-q', '--quiet', action='store_true', help="No normal output to stdout")
    a_parser.add_argument('-v', '--verbose', action='count', help="""Print verbose output.
-v prints all Jinja snippets, regardless of errors. -vv prints full AST for each Jinja node.""", default=0)
    group_analysis = a_parser.add_argument_group(
        'Analysis options',
        description='''Dumps a JSON dictionary with the results of various analysis steps.
Note that these only work for files that can be parsed without errors.
Use -q to print ONLY this JSON summary.''')
    group_analysis.add_argument(
        '-e', '--external', action='store_true',
        help='''List external variables used.''')
    group_analysis.add_argument(
        '-t', '--tags', action='store_true',
        help='''List encountered tags.''')
    args = a_parser.parse_args()

    if args.quiet:
        output = lambda *x, **kw: None

    if args.context_lines:
        LAST_THRESHOLD = args.context_lines
    if args.verbose:
        verbosity = args.verbose

    error = False
    # we do not support any flags, so for now we just:
    for filename in args.FILE:
        if '--' == filename: continue
        error |= lint(filename)
    class SetEncoder(json.JSONEncoder):
        '''https://stackoverflow.com/a/8230505'''
        def default(self, obj):
            if isinstance(obj, set): return sorted(list(obj))
            return json.JSONEncoder.default(self, obj)
    json_dump = {}
    if args.external:
        # TODO should this really be print() ?
        json_dump['external_variables'] = EXTERNAL_VARIABLES
    if args.tags:
        json_dump['files_to_tags'] = SEEN_TAGS
        tags_to_files = dict()
        for fn,tags in SEEN_TAGS.items():
            for tag in tags:
                tags_to_files[tag] = tags_to_files.get(tag, set())
                tags_to_files[tag].add(fn)
        json_dump['tags_to_files'] = tags_to_files
    if args.tags or args.external:
        print(json.dumps(json_dump, cls=SetEncoder, indent=2))

    missing_anchors = set(ALIASED_ANCHORS).difference(set(ANCHORS))
    if missing_anchors:
        # ALIASED_ANCHORS contains something not in ANCHORS, which means we are referring to
        # an anchor that doesn't exist.
        # TODO: this heuristic will let some problems fall through the cracks because we do not
        # track scoping of aliases/anchors like Ansible would, but at least we can catch
        # misspelled anchors. :-)
        error = True
        unused_anchors = set(ANCHORS).difference(set(ALIASED_ANCHORS))
        output(Colored('undefined anchors attempted aliased:', 'ERROR'))
        for m in missing_anchors:
            suggested = difflib.get_close_matches(m, unused_anchors, 1, cutoff=0.20)
            output(Colored('- '+repr(m)+str(ALIASED_ANCHORS[m].start_mark), 'ERROR'),
		end='')
            if suggested:
                output(Colored(' - did you mean ' + repr(suggested[0]), 'ERROR'), end='')
            output()

    sys.exit(error)
