#!/bin/zsh
cd -- "${0:A:h}" || exit 1
.venv/bin/recruiter setup
result=$?
if [[ $result -eq 0 ]]; then
  read 'reply?Activate background monitoring now? [y/N] '
  if [[ "$reply" == [yY] ]]; then
    .venv/bin/recruiter start
  fi
fi
read 'reply?Press Enter to close. '
