"""These run with or without the cryptography package installed (they simulate its absence)."""
import os
import subprocess
import sys

def test_missing_cryptography_gives_a_clear_error():
    code = ("import sys; sys.modules['cryptography']=None\n"
            "try:\n    import ragleap_security.crypto\nexcept ImportError as e:\n    print(e)\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin"})
    assert "ragleap-security[crypto]" in r.stdout, r.stderr


def test_core_import_does_not_need_cryptography():
    code = ("import sys; sys.modules['cryptography']=None\nimport ragleap_security\n"
            "print('ok', 'ragleap_security.crypto' in sys.modules)\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin"})
    assert r.stdout.strip() == "ok False", r.stderr
