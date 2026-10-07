### Destaques

- As Configurações foram reconstruídas com uma interface responsiva, que se adapta melhor a janelas estreitas e mantém diálogos, rolagem e controles de entrada mais consistentes em todo o aplicativo.
- Adicionada a busca de congregações na configuração inicial e nas configurações de reuniões. O Solin agora pode preencher automaticamente os dias e horários publicados das reuniões de meio de semana e do fim de semana, mantendo também a entrada manual.
- Mídia e Cenas agora usam um mecanismo libobs supervisionado, unificando reprodução, projeção, câmeras, fontes RTSP, transições, gravação, prévias, miniaturas e câmera virtual nas plataformas compatíveis.
- A sincronização de pastas vinculadas foi reformulada com operações duráveis e reconciliação determinística, para que pastas colaborativas de reuniões e playlists se recuperem e convirjam com mais confiabilidade após alterações simultâneas ou interrompidas.
- As atualizações do aplicativo passaram a usar releases verificadas do GitHub, com downloads retomáveis e um fluxo unificado de instalação nas plataformas compatíveis.

### Mídia e projeção

- Adicionadas ações de contexto de mídia para enviar itens a destinos compatíveis e definir conteúdo da tela ociosa diretamente pelos menus de playlists e reuniões.
- Playlists e pastas vinculadas agora podem conter ocorrências repetidas do mesmo vídeo compartilhando o mesmo recurso de mídia. Em reuniões, o Solin continua evitando duplicações indesejadas.
- Corrigidas as ações de imagem no navegador integrado em páginas que usam URLs relativas, inclusive quando a página define uma URL base própria.

### Cenas, câmeras e saída ao vivo

- O novo runtime baseado em libobs mantém composição de Cenas, reprodução de mídia, câmeras, fontes RTSP, transições, gravação do Programa, seleção de dispositivos de áudio, prévias, miniaturas, projeção e câmera virtual sob um único mecanismo supervisionado.

### Reuniões, playlists e cronômetros

- Corrigidas colisões de identidade em grupos de publicações que podiam exibir opções incorretas de restauração ou criar identidades duplicadas depois de restaurar conteúdo de reunião. Árvores salvas e estados antigos de sincronização são migrados preservando edições manuais e o estado das mídias.
- Adicionada uma página responsiva de configurações avançadas do cronômetro, com melhor uso dos controles de semana e parte em layouts estreitos.
- Melhorado o ciclo de vida das pastas vinculadas: desativar a sincronização remove o estado interno de sincronização sem apagar as mídias que já estão visíveis na reunião ou playlist.

### Atualizações e confiabilidade

- O Solin agora usa um manifesto centralizado de release com hashes verificados dos arquivos para descoberta e instalação de atualizações.
