As **Cenas do Solin** saíram da fase beta. Prepare os enquadramentos com antecedência e troque de cena na projeção e na câmera virtual de forma independente, o que também ajuda na operação de reuniões em língua de sinais. Controle o cronômetro pelo celular ou tablet com o **Solin Timer** e, no Windows, acompanhe as mãos levantadas no Zoom pelo Solin.

### Notas de atualização

> [!IMPORTANT]
>
> **Se você já usa o controle remoto de mídias**, ative **Mídia** em **Configurações → Acesso remoto** e configure o login novamente. Use o novo endereço exibido ali para acessar o **Solin Media** e reinstalar o aplicativo caso já o tivesse instalado. Se você já instalou o certificado, não precisa instalá-lo novamente.

### Adicionado

- **Cronômetro**
  - Com o **Solin Timer**, o presidente da reunião ou outro irmão designado pode controlar o cronômetro pelo celular ou tablet na mesma rede do Solin, enquanto o operador cuida do áudio e do vídeo. O relógio e a contagem regressiva aparecem em tela cheia sem login. Entre para iniciar, parar ou ajustar o cronômetro.
  - O Solin preenche automaticamente as partes e os tempos previstos da reunião de meio de semana com base na apostila.
  - Para avisar quem está na tribuna que alguém quer comentar, selecione **Mostrar mão no cronômetro** ao lado de **Exportar PDF**. O aviso aparece no monitor do cronômetro ou no Solin Timer, mesmo sem o Zoom.
- **Zoom (Windows)**
  - Ative o monitoramento na aba **Mãos levantadas** do Solin para ver quem quer comentar e se o microfone da pessoa está silenciado. Pela lista, você pode pedir que a pessoa ative o microfone, abaixar a mão ou silenciar e abaixar a mão de uma vez. Mantenha **Participantes** aberto no Zoom para acompanhar a lista. Você também pode exibir os nomes na tela do cronômetro.
  - Ative **Silenciar microfone durante a reprodução** para silenciar o microfone do salão no Zoom enquanto o Solin reproduz uma mídia com som. Ao pausar ou encerrar a reprodução, o Solin reativa o microfone se foi ele que o silenciou.
- Em **reuniões e playlists**, use **Excluir mídias adicionais** para remover de uma vez o que foi adicionado a uma seção e às suas subseções, preservando a estrutura, os marcadores e as mídias oficiais da reunião. Se quiser excluir a própria seção, escolha se prefere manter ou excluir as mídias que estão nela.
- Em **Configurações → Sobre**, consulte os requisitos de cada versão e veja se o seu sistema os atende. Se houver um pacote para sua plataforma e arquitetura, abra a página de download pelo histórico.

### Alterado

- **Novos perfis** usam as Cenas do Solin por padrão. Abra **Cenas** para combinar câmeras e mídias e, com câmeras PTZ compatíveis, automatizar o enquadramento ao colocar uma cena no ar. A integração com o OBS segue disponível em Configurações.
- No **Acesso remoto**, ative e configure o login do **Solin Media** e do **Solin Timer** separadamente. Os dois podem ser instalados no mesmo aparelho, e a barra de ferramentas mostra quais dispositivos fizeram login em cada um.

### Corrigido

- As **câmeras IP via RTSP** têm menos atraso na imagem ao vivo. Isso ajuda a aproximar a imagem do áudio do salão para quem assiste pelo Zoom.
- No **Zoom para Windows**, silenciar todos os participantes ou pedir que ativem o microfone funciona mesmo quando **Participantes** está no menu de opções adicionais da barra compacta.
- Os **painéis da barra de ferramentas** ficam abertos quando você alterna para outro aplicativo. Um clique em outro controle do Solin fecha o painel e aciona o controle.
- A **sincronização de pastas vinculadas** lida melhor com mudanças passageiras nos arquivos durante a configuração. Se houver uma falha temporária ao salvar, o Solin guarda as edições da reunião e tenta de novo. Mídias oficiais restauradas continuam visíveis quando chegam alterações mais antigas de outro computador.
- Os **controles de NDI** voltaram a aparecer nas configurações do OBS do Solin, permitindo escolher a transmissão que será projetada.
- O Solin voltou a buscar automaticamente o **texto do ano**.
