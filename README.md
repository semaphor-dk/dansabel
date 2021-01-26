# dansabel

Dansabel is a suite of static analysis tools for pre-flight checking of [Ansible](https://www.ansible.com) projects.

The aim is to provide an extendable complement to [Ansible-Lint](https://ansible-lint.readthedocs.io/en/latest/), not a replacement.

It currently consists of:
- a Python script that will use the YAML parser ([ruamel](https://pypi.org/project/ruamel.yaml/) as used in Ansible) to parse your YAML files
- a simplistic [Jinja2](https://en.wikipedia.org/wiki/Jinja_(template_engine)) linter using the built-in Jinja2 lexer/parser to attempt to detect typos and errors
- a git [pre-commit hook](https://githooks.com/) to launch the other scripts on patches staged for commit to your git repository

The name *dansabel* is the Danish translation of "danceable" - it helps detect when your Ansible playbooks are about to be played out of tune.

Dansabel is written and published by [Semaphor](https://semaphor.dk). Founded in 1992 it is a Danish software consultancy and hosting provider that aims to support an open, free, and decentralized internet through its participation in the FOSS communities and using FOSS software to the benefit of our customers. When not faced with global pandemics you can meet us at [BornHack](https://bornhack.dk), CCC, and other community camps, but for the time being we'll have to make do with [email](mailto:info@semaphor.dk) and Jitsi calls. In any case, feel free to contact us.

![Screenshot of commandline usage](https://user-images.githubusercontent.com/69192941/105885543-8ce93d00-6009-11eb-9b4e-4cdfc8080dfc.png)

*Screenshot: The Jinja linter in action highlighting a problem. The first excerpt shows the template contents with lexer token highlighting; the second shows the same section, but listed by individual tokens.*

## License

Dansabel is published as free software under the [ISC license](https://en.wikipedia.org/wiki/ISC_license). If you have questions or concerns about licensing, please [get in touch](mailto:info@semaphor.dk). We don't bite. :-)

## Linter

```
usage: jinjalint.py [YAML-FILES]

Lints each of the provided YAML-FILES in turn.
```

## Git hook

The `pre-commit.sh` script can be used a `pre-commit` git hook.

This hook will run on every invocation of `git commit`, and will block the commit operation if it finds a problem and returns a non-zero exit code.
In case of false positives this behaviour can be bypassed using:
```shell
git commit --no-verify
```

The script will match the extensions of modified files and call out various other tools:
- The `jq` tool to verify that JSON files are syntactically valid (`.json`)
- The `shellcheck` tool to lint shellscripts (`.sh`)
- The `ansible-lint` tool to lint YAML files (`.yml`)
- The linter script contained in this repository to validate YAML files (`.yml`) and Jinja templates (contained in the YAML files or inside `templates/` directories, as used by Ansible).

## Installation

A number of prerequisites are needed:
```shell
sudo apt install jq shellcheck python3-ruamel.yaml ansible-lint
```

You can either install it on a per-repository basis by making a symlink from `.git/hooks/pre-commit` to `pre-commit.sh` in this directory, or as a global hook across all your git repositories.

To configure a hook for a given repository:
```shell
ln -s $(pwd)/pre-commit.sh /path/to/repo/.git/hooks/pre-commit
```

To configure a global hook you can add an entry like this to your `~/.gitconfig` file:
```
[core]
    hooksPath = ~/path/to/dansabel-repo/
```
