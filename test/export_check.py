import os, importlib.util
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TX_NO_WINDOW"] = "1"
spec = importlib.util.spec_from_file_location("tx", os.path.join(APP, "transcriber.pyw")); tx = importlib.util.module_from_spec(spec); spec.loader.exec_module(tx)
s = tx.list_sessions(); print("sessions:", len(s))
if s:
    for f in ("md", "txt"):
        p = tx.export_session(s[0]["name"], f); print("exported", p, os.path.getsize(p), "bytes")
