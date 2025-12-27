import importlib
import importlib.util
import sys, os

print("CWD:", os.getcwd())
print("PYTHONPATH ok?", "C:\\Projects\\Reflex2.2" in sys.path)

spec1 = importlib.util.find_spec("trader")
spec2 = importlib.util.find_spec("trader.app")

print("has trader?", spec1 is not None)
print("has trader.app?", spec2 is not None)

if spec2:
    m = importlib.import_module("trader.app")
    print("module:", getattr(m, "__file__", None))
    print("has 'app' object:", hasattr(m, "app"))
else:
    print("Could not import trader.app — path or module issue")
