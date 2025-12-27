# DbManager/__init__.py
# Auto-load .env from the project root whenever you do: python -m DbManager.<script>
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass
