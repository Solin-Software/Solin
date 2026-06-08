import os
import hashlib
import shutil
import argparse
from concurrent.futures import ThreadPoolExecutor

def calcular_hash(caminho_arquivo, algoritmo='sha256'):
    hash_func = hashlib.new(algoritmo)
    try:
        with open(caminho_arquivo, 'rb') as f:
            for bloco in iter(lambda: f.read(8192), b''):
                hash_func.update(bloco)
        return hash_func.hexdigest()
    except Exception as e:
        print(f"[ERRO] Falha ao ler {caminho_arquivo}: {e}")
        return None

def mapear_diretorio(diretorio):
    mapa = {}
    for raiz, _, arquivos in os.walk(diretorio):
        for arquivo in arquivos:
            caminho_completo = os.path.join(raiz, arquivo)
            caminho_relativo = os.path.relpath(caminho_completo, diretorio)
            mapa[caminho_relativo] = caminho_completo
    return mapa

def deve_ignorar(rel_path, ignorar_ext):
    return any(rel_path.endswith(ext) for ext in ignorar_ext)

def comparar_diretorios(dir_old, dir_new, dir_diff, ignorar_ext, workers):
    mapa_old = mapear_diretorio(dir_old)
    mapa_new = mapear_diretorio(dir_new)

    def processar(rel_path):
        if deve_ignorar(rel_path, ignorar_ext):
            return None

        caminho_new = mapa_new[rel_path]
        caminho_old = mapa_old.get(rel_path)

        if caminho_old is None:
            return ("NOVO", rel_path, caminho_new)

        hash_new = calcular_hash(caminho_new)
        hash_old = calcular_hash(caminho_old)

        if hash_new and hash_old and hash_new != hash_old:
            return ("MODIFICADO", rel_path, caminho_new)

        return None

    resultados = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for resultado in executor.map(processar, mapa_new.keys()):
            if resultado:
                resultados.append(resultado)

    for tipo, rel_path, origem in resultados:
        print(f"[{tipo}] {rel_path}")
        destino = os.path.join(dir_diff, rel_path)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copy2(origem, destino)

    print(f"\nTotal de arquivos copiados: {len(resultados)}")

def main():
    parser = argparse.ArgumentParser(
        description="Compara dois diretórios por hash e gera um diff com base no diretório novo."
    )

    parser.add_argument("--old", required=True, help="Diretório antigo")
    parser.add_argument("--new", required=True, help="Diretório novo (prioridade)")
    parser.add_argument("--out", required=True, help="Diretório de saída (diff)")
    parser.add_argument("--ignore-ext", nargs="*", default=[], help="Extensões para ignorar (.log .tmp)")
    parser.add_argument("--workers", type=int, default=4, help="Número de threads")

    args = parser.parse_args()

    if not os.path.exists(args.old):
        print("Erro: diretório OLD não existe")
        return

    if not os.path.exists(args.new):
        print("Erro: diretório NEW não existe")
        return

    os.makedirs(args.out, exist_ok=True)

    comparar_diretorios(
        args.old,
        args.new,
        args.out,
        args.ignore_ext,
        args.workers
    )

    print("\nConcluído!")

if __name__ == "__main__":
    main()