import os
from PIL import Image

PASTA_ORIGEM = "imagens_originais"
PASTA_DESTINO = "imagens_96x96"
TAMANHO = 96

def processar_imagens():
    if not os.path.exists(PASTA_ORIGEM):
        print(f"Erro: A pasta '{PASTA_ORIGEM}' não foi encontrada.")
        return
    
    # Incluídos formatos adicionais como .webp e .jfif
    extensoes_validas = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".jfif")
    contador_total = 0
    total_arquivos = 0
    
    print("A analisar a pasta de origem...")
    for raiz, diretorios, arquivos in os.walk(PASTA_ORIGEM):
        rel_path = os.path.relpath(raiz, PASTA_ORIGEM)
        destino_atual = os.path.join(PASTA_DESTINO, rel_path) if rel_path != "." else PASTA_DESTINO
        os.makedirs(destino_atual, exist_ok=True)
        
        for nome_ficheiro in arquivos:
            total_arquivos += 1
            if nome_ficheiro.lower().endswith(extensoes_validas):
                caminho_entrada = os.path.join(raiz, nome_ficheiro)
                caminho_saida = os.path.join(destino_atual, nome_ficheiro)
                
                try:
                    with Image.open(caminho_entrada) as img:
                        largura, altura = img.size
                        
                        # Recortar ao centro para ficar quadrado
                        min_lado = min(largura, altura)
                        esquerda = (largura - min_lado) / 2
                        topo = (altura - min_lado) / 2
                        direita = (largura + min_lado) / 2
                        baixo = (altura + min_lado) / 2
                        
                        img_quadrada = img.crop((esquerda, topo, direita, baixo))
                        img_final = img_quadrada.resize((TAMANHO, TAMANHO), Image.Resampling.LANCZOS)
                        img_final.save(caminho_saida)
                        contador_total += 1
                        print(f"Sucesso: {rel_path}/{nome_ficheiro}")
                        
                except Exception as e:
                    print(f"Erro ao ler o ficheiro {nome_ficheiro}: {e}")
            else:
                print(f"Ignorado (formato não suportado): {nome_ficheiro}")
                
    print(f"\n--- Resumo ---")
    print(f"Total de ficheiros encontrados: {total_arquivos}")
    print(f"Total de imagens redimensionadas: {contador_total}")

if __name__ == "__main__":
    processar_imagens()