# Auto-load .env for ANY python process launched in this folder
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass
