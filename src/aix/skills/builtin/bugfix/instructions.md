# Fix a bug
Find the root cause before changing code.

- Reproduce the failure, ideally with a failing test.
- Fix the cause, not the symptom, with the smallest change that works.
- Keep a regression test that fails without your fix.
- Stay inside your file scope. Do not claim success; the checks decide.
