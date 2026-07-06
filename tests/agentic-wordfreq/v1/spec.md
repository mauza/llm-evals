# wordfreq

Build a command-line tool `wordfreq.py` in the current directory.

## Behavior

`python3 wordfreq.py <file> <n>` prints the `n` most frequent words in the
file, one per line, as `<word> <count>`.

- A "word" is a maximal run of ASCII letters; matching is case-insensitive
  and output words are lowercase.
- Sort by count descending, then alphabetically for ties.
- If the file does not exist, print `error: file not found` to stderr and
  exit with code 2.
- If `n` is larger than the number of distinct words, print them all.

## Acceptance criteria

- Works on `sample.txt` in this directory (try it yourself).
- Handles the missing-file case exactly as specified.
- No third-party dependencies; Python stdlib only.
