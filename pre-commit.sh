#!/usr/bin/env bash

# exit on error
set -eu

afslut()
{
    # reset colors
    printf '\e[0m'
    [[ "${our_dir}" =~ "githooks-precommit" ]] && rm -rf -- "${our_dir}"
}

trap afslut EXIT
our_dir="$(mktemp -d -t 'githooks-precommit.XXXXXXXX')"

git_dir="$(git rev-parse --show-toplevel)"

########## validate staged files before commit

original_file="${*}"

fejl()
{
    echo $'\x1b[31;1m'"${origname}: rejected by pre-commit.sh: ${original_file}"$'\x1b[0m'
    exit 1
}

# make sure we have a an accurate index of the files that have changed:
git update-index -q --refresh

# When making the initial commit, there is no HEAD ref.
# In that case we need to diff the staged objects against the empty tree.
# see https://stackoverflow.com/questions/40883798/how-to-get-git-diff-of-the-first-commit#comment68984343_40884093
head_commit=$( { git rev-list --quiet HEAD --max-count=0 2>/dev/null && echo HEAD ; } || \
		   git hash-object -t tree --stdin </dev/null)

# "filter=ACM": A)dded C)opied M)odified R)enamed, not D)eleted
git diff-index --name-only --diff-filter=ACMR --cached "${head_commit}" -z \
    -- \
    | while IFS='' read -rd '' origname
do

    # if 'git commit' is being executed in subdir and not the root of the repo,
    # we have to adjust the paths we receive from Git since those are relative to
    # the repo root dir:
    origfullname="$(realpath -e --relative-to=. -- "${git_dir}/${origname}")"

    # This section is necessary to prevent e.g. the scenario where the linter complains,
    # the user corrects (but forgets to stage the changes), then commits.
    # In that case the file in the work tree (ie the checkout folder in the filesystem)
    # will be correct, but the resulting commit will not, since that is still lacking
    # the user's modifications.
    # If the user has staged a patch for inclusion in this commit, but also has
    # unstaged patches to that file, we extract the staged object to a file in /tmp
    # instead of operating on the file in the work tree:
    { 1>/dev/null cmp \
       <(git diff-index --diff-filter=ACMR "${head_commit}" -z \
	     -- "${origfullname}") \
       <(git diff-index --diff-filter=ACMR "${head_commit}" -z --cached \
	     -- "${origfullname}") && {
	  blobpath="${origfullname}"
      }
    } || {
	new_blob=$(git diff-index --diff-filter=ACMR "${head_commit}" -z --cached -- \
		       "${origfullname}" | cut -d ' ' -f 4)
	blobpath="${our_dir}/${new_blob}.$(basename "$origname")"
	git show "${new_blob}" > "${blobpath}"
    }

    # color output from this script red:
    printf '\e[31;1m'

    # note the use of ;;& which ensures $blobpath is tried against
    # all cases in order, as opposed to ;; which would terminate
    # after first successful match
    case "${origfullname}" in

	# validate that staged json files can be read by python:
	 *.json)
	     1>/dev/null jq . -- "$blobpath" \
		 || fejl
	     ;;&

	 *.sh)
	     # the excludes work around a logic bug in shellcheck:
	     shellcheck --exclude=SC2221,SC2222 -- "$blobpath" \
		 || fejl
	     ;;&

#	 *.py)
#	     pylint3 --errors-only -- "$blobpath" \
#		 || fejl
#	     ;;&

	 # validate yaml files with ansible-lint:
	 *.yml)
	     ansible-lint -- "$blobpath" \
		 || fejl
	     ;;&

	 *roles/*/templates/* ) # try to match raw templates in addition to .j2:
	     ! [[ "$(dirname "${origfullname}")" =~ templates$
		]] && break ;&
	 *.yml) ;&
         *.j2)

	     "$(dirname "$0")"/jinjalint.py -- "$blobpath" \
		 || fejl
	     ;;&

    esac
done

