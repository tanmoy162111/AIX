# Review code
Review the change independently and critically. Check correctness, error handling, tests,
readability and consistency with the surrounding code. Ignore the author's claims and judge the
diff and the check results you are given.

## Output format (required)
You may explain your reasoning in prose. End your reply with exactly one fenced JSON block:

```json
{"findings": [{"severity": "info|low|medium|high|critical", "file": "path", "line": 1,
  "title": "short title", "detail": "what is wrong and why", "confidence": 0.0}]}
```

Use an empty list when you find nothing. `confidence` is 0.0 to 1.0. Do not modify any files.
