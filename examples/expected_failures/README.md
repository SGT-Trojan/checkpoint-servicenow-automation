# See the Safety Checks Stop Bad Input

This offline example confirms exact failures without opening a connection:

| Case | Expected result |
|---|---|
| Placeholder SHA256 | `ValueError` containing `invalid SHA256` |
| Non-IP resolver target | Exit 5 |
| Missing resolver arguments | Exit 64 |
| Missing Take for member assertion | Exit 2 |
| Direct helper without execution approval | Exit 3, planned only |

```bash
python3 examples/expected_failures/check_failures.py
```

The script exits zero only when every guard behaves as documented.
