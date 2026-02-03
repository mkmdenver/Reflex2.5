import pathlib
p = pathlib.Path(r"c:\Projects\Reflex2.5\trader\app.py")
s = p.read_text(encoding="utf-8", errors="replace")
anchor = "            snapshot_for_account=_snapshot_for_account,"
if anchor not in s: raise SystemExit("Anchor not found")
inject = anchor + "\n            instance_id=os.getenv(\"REFLEX_INSTANCE_ID\", \"live\"^)," + "\n            redis_url=(os.getenv(\"GARNET_URL\"^) or os.getenv(\"REDIS_URL\"^) or os.getenv(\"REFLEX_REDIS_URL\"^) or \"redis://127.0.0.1:6379/0\"^),"
s2 = s.replace(anchor, inject, 1)
if s2 == s: raise SystemExit("No change applied")
p.write_text(s2, encoding="utf-8")
print("Patched:", p)
