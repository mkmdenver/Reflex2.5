import os
import ast
import sys

def extract_imports_from_file(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=file_path)
        except SyntaxError:
            return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split('.')[0])
    return imports

def find_python_files(directory):
    py_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))
    return py_files

def generate_requirements(directory="."):
    all_imports = set()
    py_files = find_python_files(directory)
    for file_path in py_files:
        all_imports.update(extract_imports_from_file(file_path))

    # Basic filter for standard library modules
    stdlib_modules = {
        'os', 'sys', 'math', 'json', 're', 'datetime', 'time', 'random',
        'collections', 'itertools', 'functools', 'subprocess', 'threading',
        'multiprocessing', 'logging', 'pathlib', 'shutil', 'typing', 'unittest',
        'http', 'email', 'csv', 'argparse', 'asyncio', 'queue', 'socket',
        'traceback', 'pprint', 'statistics', 'decimal', 'fractions', 'heapq',
        'bisect', 'copy', 'enum', 'inspect', 'io', 'zipfile', 'tarfile'
    }

    third_party_imports = sorted(all_imports - stdlib_modules)

    with open("requirements.txt", "w", encoding="utf-8") as f:
        for pkg in third_party_imports:
            f.write(pkg + "\n")

    print(f"✅ requirements.txt generated with {len(third_party_imports)} packages.")

if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    generate_requirements(target_dir)