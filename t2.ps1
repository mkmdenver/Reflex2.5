import importlib, sys, os
print("CWD:", os.getcwd())
print("PYTHONPATH ok?", "C:\\Projects\\Reflex2.2" in sys.path)
print("has trader?", importlib.util.find_spec("trader") is not None)
print("has trader.app?", importlib.util.find_spec("trader.app") is not None)
m = importlib.import_module("trader.app")
print("module:", getattr(m, "__file__", None))
print("has 'app' object:", hasattr(m, "app"))