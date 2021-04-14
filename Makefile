default:
	#

test:
	@find testcases/bad -type f -name '*.yml' '(' \
	-exec ./jinjalint.py -q '{}' ';' \
	-and -printf 'FAIL %p\n' \
	-or -printf 'OK %p\n' ')'
	@find testcases/good -type f -name '*.yml' '(' \
	-exec ./jinjalint.py -q '{}' ';' \
	-and -printf 'OK %p\n' \
	-or -printf 'FAIL %p\n' ')'
