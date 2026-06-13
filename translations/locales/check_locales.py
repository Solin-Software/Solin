import os
import json
import glob
import ast
import re
import sys

def flatten_dict(d, parent_key='', sep='.'):
    """Achata dicionários aninhados para comparação profunda."""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)

def check_locales(json_files):
    """Verifica se todos os JSONs têm exatamente as mesmas chaves."""
    file_keys = {}
    all_keys = set()
    
    for filepath in json_files:
        filename = os.path.basename(filepath)
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    continue
                flat_keys = set(flatten_dict(data).keys())
                file_keys[filename] = flat_keys
                all_keys.update(flat_keys)
        except Exception as e:
            print(f"❌ Erro ao ler {filename}: {e}")
            return False, set()

    print(f"📊 Analisando {len(file_keys)} arquivos de idioma...\n")
    
    is_identical = True
    for filename, keys in file_keys.items():
        missing = all_keys - keys
        if missing:
            is_identical = False
            print(f"⚠ {filename} está faltando {len(missing)} chave(s):")
            for k in sorted(missing):
                print(f"   - {k}")
            print()

    if is_identical:
        print("✅ Fase 1: Todos os JSONs são idênticos! Nenhuma chave faltando.\n")
        
    return is_identical, all_keys

def find_unused_keys(all_keys, source_dir, detailed=False):
    """
    Procura as chaves recursivamente em TODOS os arquivos .py dentro de src/solin/.
    Usa 'ast' (Abstract Syntax Tree) como método principal.
    Fallback para regex caso o arquivo tenha SyntaxError no AST.

    Se detailed=True, exibe ao final todos os locais (arquivo:linha) onde
    cada chave foi encontrada no código.
    """
    if not os.path.exists(source_dir):
        print(f"❌ Diretório '{source_dir}' não encontrado.")
        return

    print(f"🔍 Fase 2: Varrendo código-fonte em '{source_dir}' e TODAS as suas subpastas...")
    
    scanned_files = 0
    skipped_files = []
    all_python_strings = set()

    # Mapeia string -> lista de (filepath, linha) — preenchido sempre que detailed=True
    string_locations: dict = {} if detailed else None
    
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not d.startswith('__') and not d.startswith('.')]
        
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        code_content = f.read()
                        scanned_files += 1
                        
                        try:
                            # Tenta AST primeiro (mais preciso)
                            tree = ast.parse(code_content)
                            for node in ast.walk(tree):
                                value = None
                                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                                    value = node.value
                                elif isinstance(node, getattr(ast, 'Str', type(None))):
                                    value = node.s

                                if value is not None:
                                    all_python_strings.add(value)
                                    if detailed:
                                        string_locations.setdefault(value, []).append(
                                            (filepath, node.lineno)
                                        )

                        except SyntaxError:
                            # Fallback: extrai strings por regex se AST falhar
                            print(f"⚠ [DEBUG] ARQUIVO IGNORADO PELO AST (SyntaxError): {filepath}")
                            skipped_files.append(filepath)
                            for line_num, line in enumerate(code_content.splitlines(), start=1):
                                for match in re.finditer(r'["\']([^"\']+)["\']', line):
                                    value = match.group(1)
                                    all_python_strings.add(value)
                                    if detailed:
                                        string_locations.setdefault(value, []).append(
                                            (filepath, line_num)
                                        )

                except Exception as e:
                    print(f"⚠ Aviso: Não foi possível ler {filepath}: {e}")

    if scanned_files == 0:
        print("❌ Nenhum arquivo .py encontrado na pasta src/solin ou subpastas.")
        return

    if skipped_files:
        print(f"\n⚠ [DEBUG] {len(skipped_files)} arquivo(s) usaram fallback por regex (SyntaxError no AST):")
        for f in skipped_files:
            print(f"   - {f}")
        print()

    unused_keys = []

    for key in all_keys:
        parts = key.split('.')
        sub_key = ".".join(parts[1:]) if len(parts) > 1 else key

        matched_as = None
        if key in all_python_strings:
            matched_as = key
        elif sub_key in all_python_strings:
            matched_as = sub_key
        elif key.startswith("meta."):
            meta_key = key.replace("meta.", "")
            if meta_key in all_python_strings:
                matched_as = meta_key

        if not matched_as:
            unused_keys.append(key)

    print(f"📄 Arquivos Python verificados: {scanned_files}")
    
    if unused_keys:
        print(f"\n🧹 Atenção! Encontradas {len(unused_keys)} chave(s) que não parecem estar sendo usadas no código:")
        for k in sorted(unused_keys):
            print(f"   - {k}")
        print("\n*Nota: Chaves com variáveis (ex: f'ui.col_{nome}'), elas podem aparecer aqui.*")
    else:
        print("\n🎉 Perfeito! Todas as chaves do JSON estão sendo utilizadas no código.")

    # ── Modo detalhado: mostra onde cada chave aparece no código ──────────────
    if detailed:
        print(f"\n{'='*60}")
        print(f"📍 DETALHES: localização de cada chave no código")
        print(f"{'='*60}")

        for key in sorted(all_keys):
            parts = key.split('.')
            sub_key = ".".join(parts[1:]) if len(parts) > 1 else key

            matched_as = None
            if key in all_python_strings:
                matched_as = key
            elif sub_key in all_python_strings:
                matched_as = sub_key
            elif key.startswith("meta."):
                meta_key = key.replace("meta.", "")
                if meta_key in all_python_strings:
                    matched_as = meta_key

            if matched_as and matched_as in string_locations:
                locations = string_locations[matched_as]
                print(f"\n  🔑 {key}")
                for filepath, lineno in locations:
                    rel_path = os.path.relpath(filepath, os.path.dirname(source_dir))
                    print(f"     📄 {rel_path}:{lineno}")
            else:
                print(f"\n  ❌ {key}  ← não encontrada no código")

def main():
    detailed = "--details" in sys.argv

    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_pattern = os.path.join(current_dir, '*.json')
    json_files = glob.glob(json_pattern)

    if not json_files:
        print("❌ Nenhum arquivo .json encontrado neste diretório.")
        return

    is_identical, all_keys = check_locales(json_files)

    if is_identical:
        project_root = os.path.dirname(os.path.dirname(current_dir))
        source_dir = os.path.join(project_root, 'src', 'solin')
        
        find_unused_keys(all_keys, source_dir, detailed=detailed)
    else:
        print("\n🛑 Verificação de uso no código cancelada. Corrija as traduções faltantes primeiro.")

if __name__ == "__main__":
    main()
