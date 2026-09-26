# Security review
Look for injection, unsafe deserialization, path traversal, authentication and authorization gaps,
secret handling mistakes, unsafe subprocess or network use, and risky dependencies. Report only
issues you can tie to a file and line.

## Output format (required)
You may explain your reasoning in prose. End your reply with exactly one fenced JSON block:

```json
{"findings": [{"severity": "info|low|medium|high|critical", "file": "path", "line": 1,
  "title": "short title", "detail": "what is wrong and why", "confidence": 0.0}]}
```

Use an empty list when you find nothing. `confidence` is 0.0 to 1.0. Do not modify any files.
