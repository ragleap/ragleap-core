# Your first hour

`ragleap-physical` is pre-alpha and simulation only. Nothing here controls real hardware.

## 1. Read (10 minutes)

Read `README.md`, then the design note it links to. Note what is *not* built.

## 2. Install and test (10 minutes)

You need Python 3.10 or newer. From `packages/ragleap-physical`:

```
pip install -e ".[test]"
pytest -q
```

Everything should pass. Tests that need MuJoCo or the MCP SDK are skipped unless you install those extras. A skipped test cannot catch anything, so do not count it as evidence.

## 3. Run the demo (5 minutes)

```
python examples/demo_fake.py
```

It walks the whole path with a built-in one-joint simulator: observe, propose, a named person approves, apply, check the real outcome. At the end, an out-of-range value is refused, not clamped.

## 4. Break one limit on purpose (20 minutes)

This shows that a test really guards a limit. Do it in a scratch copy, never in your branch:

**macOS / Linux:**

```bash
cp -r . /tmp/scratch && cd /tmp/scratch
find . -name __pycache__ -prune -exec rm -rf {} +
export PYTHONDONTWRITEBYTECODE=1
```

**Windows PowerShell:**

```powershell
$scratch = Join-Path $env:TEMP ("ragleap-physical-scratch-" + [guid]::NewGuid().ToString("N"))
Copy-Item -Path . -Destination $scratch -Recurse
Set-Location $scratch

Get-ChildItem -Path . -Directory -Filter "__pycache__" -Recurse -Force |
    Remove-Item -Recurse -Force
$env:PYTHONDONTWRITEBYTECODE = "1"
```

In the copy, open `src/ragleap_physical/envelope.py`. Find:

```python
if not lim["pos_min"] <= p.value <= lim["pos_max"]:
```

Change it to `if False:`, then run `pytest -q`. Tests must now fail, including `test_out_of_range_refused_no_clamping`. If nothing fails, the test is too weak: that is a finding worth reporting.

## 5. Before your first pull request

- Ask first before changing `envelope.py`, `bridge.py`, `mcp_server.py` or `tools.py`. They are the safety layer.
- Every new limit or check needs a test that fails when you break it on purpose.
- Tests never touch hardware or a network. Use a fake clock, never `sleep`.
- No secrets, no addresses, no claims about real machines.
- Say in the pull request if the work is AI-assisted.
- Write four parts: What, Limits, Tested, Not tested yet.
