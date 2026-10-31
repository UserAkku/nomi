# minihttp

A deliberately tiny C HTTP client library. It exists only so the Nomi example in
`examples/fabricated-security-report/` runs offline (for instance in CI). It is
not a real project.

Build: `cc -Iinclude lib/*.c tests/test_url.c -o test_url && ./test_url`
