Las Escenas de Solin llegan a macOS: combina cámaras y contenido multimedia y elige qué mostrar en el proyector y en Zoom, con una escena independiente para cada salida. También es más fácil preparar el contenido multimedia de los discursos públicos, manejar cámaras PTZ y grabar reuniones con archivos más pequeños. En Windows, se corrigieron ruidos de fondo que podían aparecer al reproducir videos y audios en equipos menos potentes.

### Notas de actualización

> [!IMPORTANT]
>
> - **Linux:** Comprueba tu sistema antes de actualizar: esta versión requiere glibc 2.43 o posterior, con Ubuntu 26.04 como referencia. Si usas Ubuntu 24.04 o anterior, actualiza el sistema para utilizar esta versión de Solin. Usa X11 o Wayland con XWayland.
> - **Cámara virtual en macOS:** Para enviar las escenas a Zoom, instala OBS, habilita la extensión **OBS Virtual Camera** y selecciona esa cámara en Zoom. Detén la salida en OBS antes de activarla en Solin. La extensión no es necesaria para mostrar escenas en televisores o en un proyector.

### Añadido

- **Escenas de Solin:**
  - En macOS, envía la composición completa de las escenas a la proyección y a la cámara virtual. Por ejemplo, muestra contenido multimedia en el proyector y una composición con cámara en Zoom, controlando cada salida por separado.
  - Supervisa una escena en una ventana más grande mientras trabajas en otras partes de Solin. Con cámaras PTZ compatibles, mueve la cámara, ajusta el zoom y guarda posiciones con miniaturas para reconocer cada encuadre. Solin pide confirmación antes de mover una cámara que está al aire.
  - Añade cámaras IP compatibles con ONVIF mediante la búsqueda en la red o por dirección. Elige una transmisión de video encontrada y deja que Solin complete los ajustes RTSP y PTZ.
  - Cambia de encuadre con la transición **Fusionar**: una cámara compartida por ambas escenas se mueve y cambia de tamaño suavemente hasta su nueva posición. Las fuentes que entran o salen de la composición aparecen o desaparecen de forma gradual.
- **Publicaciones:**
  - Añade solo el contenido multimedia que necesitas: al abrir un archivo `.jwpub`, revisa su contenido y selecciona las imágenes, los videos o los audios que quieras insertar en la reunión o lista de reproducción. La selección también funciona con archivos descargados mediante el navegador de Solin.
  - Para preparar un discurso público, abre **Multimedia de discursos públicos…**, indica el archivo S-34mp con el contenido multimedia de los bosquejos y selecciona lo que vas a utilizar. Solin recuerda el archivo elegido y, en las reuniones de fin de semana, coloca el contenido en la sección del discurso público.
- **Zoom (Windows):**
  - Prepara Zoom con **Inicio de la reunión**: Solin conecta tu audio y video y silencia a los participantes. Para conversar antes o después, **Antes/después de la reunión** desconecta tu audio y video y solicita a los participantes que activen sus micrófonos.
  - Automatiza atajos junto con estos controles: en **Atajos automáticos**, elige los eventos **Antes de la reunión**, **Inicio de la reunión** o **Fin de la reunión** para ejecutar las teclas configuradas después de las acciones de Zoom.
  - Vincula la canción de fondo a los controles para detenerla al iniciar la reunión y reproducirla antes o después. Si prefieres usar la parada programada de la canción, también puede preparar Zoom para el inicio de la reunión.
- **Actualizaciones:** Consulta los cambios de cada versión en **Ajustes → Acerca de**. Allí también puedes elegir recibir versiones beta para probar novedades antes de la versión estable; mostrar las notas de las betas en el historial es una opción independiente.

### Modificado

- **Escenas de Solin:**
  - Controla la proyección y la cámara virtual por separado mediante los botones de monitor y cámara, también en el editor. Para mostrar la misma composición en ambas salidas, selecciona la misma escena en las dos. Estos controles sustituyen **Pasar a vivo** y el vínculo de **Mostrar en las ventanas multimedia**. En el panel de escenas, muestra los controles de proyección con el botón de monitor cuando los necesites; están ocultos de forma predeterminada.
  - Accede a las escenas que quedan fuera de la parte visible del panel mediante la rueda del ratón, el panel táctil o la barra de desplazamiento. El panel flotante se adapta al espacio disponible en pantalla.
  - Crea encuadres que sobrepasen los bordes de la escena: mueve las fuentes parcialmente fuera del área, incluso con coordenadas negativas, y cambia su tamaño o recórtalas sin que vuelvan de golpe al interior del marco.
- **Grabación:** Graba reuniones ocupando menos espacio, con una codificación optimizada y 720p como resolución predeterminada. Elige 360p, 540p, 720p o 1080p según la nitidez y el tamaño de archivo deseados, sin cambiar la resolución de la cámara virtual enviada a Zoom.
- **Canciones de fondo:** Prueba o reproduce un video o audio sin escuchar dos sonidos a la vez: Solin silencia automáticamente la canción de fondo y restaura su volumen cuando sales del contenido multimedia. La canción sigue avanzando en silencio y no vuelve a sonar durante las pausas o la carga.
- **Almacenamiento de reuniones:** Solin aprovecha el espacio en disco de forma más inteligente: limpia las extracciones temporales, elimina de la caché las publicaciones antiguas que ya no se utilizan y guarda una sola copia de las imágenes idénticas usadas por varios perfiles. Esto puede reducir considerablemente el almacenamiento acumulado, especialmente al preparar muchas reuniones o utilizar varios idiomas y perfiles.
- **Carpetas vinculadas:** Coloca contenido multimedia en la carpeta de una reunión para que aparezca en Solin, incluso sin sincronizar la organización de la reunión entre equipos. Ahora puedes crear esa carpeta desde Solin y conservarla al desactivar la sincronización; eliminarla es una acción independiente que requiere confirmación. También puedes crear carpetas de listas de reproducción vinculadas desde la aplicación.
- **Perfiles:**
  - Encuentra y selecciona perfiles con más facilidad en ventanas pequeñas o cuando tienes muchos perfiles. La pantalla se adapta al espacio disponible, con desplazamiento y navegación por teclado.
  - Al crear un perfil, elegir la congregación completa el nombre del perfil y activa las canciones de fondo automáticas cuando hay horarios de reunión disponibles. Los nombres de perfil que hayas editado se conservan.

### Corregido

- **Cámaras:**
  - Conecta una cámara con Solin abierto y aparecerá automáticamente en la lista. En Windows, volver a conectar una cámara que ya se utiliza en una escena permite recuperar su imagen sin reiniciar Solin, incluso si estaba desconectada al abrir la aplicación.
  - Sigue las cámaras IP con menos retraso añadido por Solin: la recepción RTSP se ha ajustado para priorizar la imagen en vivo y evitar procesar el audio de la cámara. También se corrigieron la aplicación de las opciones TCP/UDP y de búfer y la visualización en las vistas previas, la proyección y las miniaturas con la cámara virtual desactivada.
- **Cámara virtual:** Identifica cuándo el video está llegando realmente a la salida. Si el envío se bloquea, Solin intenta recuperarlo automáticamente y avisa si no lo consigue, en lugar de seguir indicando que funciona sin enviar video.
- **Reproducción:** Más estabilidad al reproducir archivos locales o por la red, con correcciones de errores al cambiar la velocidad, avanzar o retroceder y reanudar tras interrupciones de conexión. También se corrigieron reinicios inesperados al terminar un archivo multimedia. En Windows, se mejoró la continuidad del audio para evitar cortes, chasquidos y ruidos de fondo que podían resultar más perceptibles en equipos menos potentes.
- **Reuniones:**
  - La opción de descargar manualmente ahora también se aplica a las publicaciones de la reunión. Antes, se seguían descargando al navegar, incluso con las descargas automáticas desactivadas. Ahora, pulsa **Descargar** cuando quieras preparar una reunión que todavía no esté disponible.
  - Las reuniones ya preparadas reciben actualizaciones de contenido y recuperan imágenes ausentes conservando tus cambios, lo que reduce la necesidad de volver a prepararlas cuando una imagen deja de aparecer.
- **Proyección:**
  - Mover el ratón a la pantalla de proyección ya no deja el puntero sobre el contenido mostrado al auditorio.
  - Al ajustar una foto o ilustración de una reunión o lista de reproducción en la vista previa interactiva, las opciones **Ajustar a la proporción de la proyección** y **Mantener la imagen cubriendo el marco** podían cambiar o restablecer el zoom y la posición de la imagen en el televisor sin pulsar **Aplicar al proyector**. Ahora, estas opciones solo ajustan la vista previa; la imagen proyectada cambia cuando aplicas los ajustes.
- **Actualizaciones:** Abre enlaces de las notas de versión sin perder el texto que estabas leyendo. En instalaciones compatibles de Windows, pulsa **Actualizar ahora** una sola vez: Solin descarga y verifica el paquete y después inicia el instalador.
- **Diálogos:** Las ventanas de notificaciones, actualizaciones y selección de destino del contenido multimedia se abren centradas en Solin, incluso después de moverlo a otro monitor.

### Eliminado

- **Zoom (Windows):** Se eliminó el recuento de asistencia porque podía omitir participantes que quedaban fuera de la parte visible de la lista de Zoom.
