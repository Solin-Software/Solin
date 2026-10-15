### Cenas e saída ao vivo

- Unificados o conteúdo em apresentação e a tela ociosa na fonte Mídia do Solin. Novas coleções começam com as cenas Mídia e Câmera, sem exigir uma cena Padrão separada.
- A projeção e a saída de câmera virtual/gravação agora mantêm seleções de cena e destinos de retorno independentes após a reprodução automática de mídia, respeitando as trocas manuais de cena.
- As mudanças entre a tela ociosa e o conteúdo apresentado agora usam a transição da cena de destino, inclusive quando ambos usam a mesma cena. Vídeos da tela ociosa se repetem sem áudio e recomeçam do início ao serem exibidos novamente.
- Novas camadas de mídia, câmera e referência a outra cena passam a ajustar a fonte inteira à camada por padrão, evitando recortes indesejados. Os enquadramentos existentes são preservados, e as opções de preencher e esticar continuam disponíveis.
- Corrigido preview de cenas que continuavam mostrando uma imagem ou um vídeo depois de seu fechamento.

### Câmeras

- Adicionados controles de resolução, taxa de quadros e formato conforme as combinações compatíveis com cada dispositivo. A seleção automática busca modos de até 1920×1080 a 30 FPS quando disponíveis; também é possível selecionar modos personalizados compatíveis. As configurações de captura escolhidas agora são aplicadas de forma consistente entre as plataformas.
- As configurações de câmera são carregadas em segundo plano e reutilizam os recursos já consultados do dispositivo. Modos salvos indisponíveis continuam identificados, e falhas de captura ou dimensões de imagem inesperadas agora apresentam informações mais claras.

### Mídia e interface

- Miniaturas, títulos de mídia e informações dos arquivos agora usam as bibliotecas de mídia incluídas no Solin, dispensando uma instalação separada do FFmpeg para esses recursos. As miniaturas priorizam a capa incorporada ao arquivo e usam um quadro do vídeo quando não há capa.
- Melhoradas a preparação e o cancelamento de conteúdo visual, incluindo o tratamento do primeiro quadro e a liberação de recursos ao substituir conteúdo ou fechar o aplicativo.
- Corrigidos botões que podiam continuar destacados após a saída do ponteiro da janela.
