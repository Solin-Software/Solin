Las **Escenas de Solin** salieron de la fase beta. Prepara los encuadres con antelación y cambia de escena en la proyección y en la cámara virtual de forma independiente, algo que también facilita la operación en reuniones en lengua de señas. Controla el cronómetro desde un celular o una tableta con **Solin Timer** y, en Windows, sigue las manos levantadas en Zoom desde Solin.

### Notas de actualización

> [!IMPORTANT]
>
> **Si ya usas el control remoto de contenido multimedia**, activa **Multimedia** en **Ajustes → Acceso remoto** y vuelve a configurar el inicio de sesión. Usa la nueva dirección que aparece allí para acceder a **Solin Media** y reinstalar la aplicación si ya la tenías instalada. Si ya instalaste el certificado, no hace falta volver a instalarlo.

### Añadido

- **Cronómetro**
  - Con **Solin Timer**, el presidente de la reunión u otro hermano encargado puede controlar el cronómetro desde un celular o una tableta en la misma red que Solin, mientras el operador se ocupa del audio y el video. El reloj y la cuenta regresiva se muestran a pantalla completa sin iniciar sesión. Inicia sesión para poner en marcha, detener o ajustar el cronómetro.
  - Solin completa automáticamente las partes y los tiempos previstos de la reunión de entre semana a partir de la guía de actividades.
  - Para avisar a quien está en la plataforma de que alguien quiere comentar, selecciona **Mostrar la mano en el cronómetro** junto a **Exportar PDF**. El aviso aparece en el monitor del cronómetro o en Solin Timer, incluso si no estás usando Zoom.
- **Zoom (Windows)**
  - Activa el seguimiento en la pestaña **Manos levantadas** de Solin para ver quién quiere comentar y si su micrófono está silenciado. Desde la lista puedes pedirle que active el micrófono, bajarle la mano o silenciar su micrófono y bajarle la mano en un solo paso. Mantén abierto **Participantes** en Zoom para seguir la lista. También puedes mostrar los nombres en la pantalla del cronómetro.
  - Activa **Silenciar el micrófono durante la reproducción** para silenciar el micrófono del salón en Zoom mientras Solin reproduce contenido multimedia con sonido. Al pausar o terminar la reproducción, Solin vuelve a activar el micrófono si lo había silenciado.
- En **reuniones y listas de reproducción**, usa **Eliminar medios adicionales** para quitar de una vez el contenido multimedia añadido a una sección y sus subsecciones, conservando la estructura, los marcadores y el contenido multimedia oficial de la reunión. Al eliminar la sección, puedes elegir si conservas o eliminas su contenido multimedia.
- En **Ajustes → Acerca de**, consulta los requisitos de cada versión y comprueba si tu sistema los cumple. Si hay un paquete para tu plataforma y arquitectura, puedes abrir su página de descarga desde el historial de versiones.

### Modificado

- Los **perfiles nuevos** usan las Escenas de Solin de forma predeterminada. Abre **Escenas** para combinar cámaras y contenido multimedia y, si usas cámaras PTZ compatibles, ajustar automáticamente el encuadre al poner una escena al aire. La integración con OBS sigue disponible en Ajustes.
- En **Acceso remoto**, activa **Solin Media** y **Solin Timer** por separado y configura el inicio de sesión de cada uno. Puedes instalar ambos en el mismo dispositivo y ver en la barra de herramientas qué dispositivos han iniciado sesión en cada aplicación.

### Corregido

- La imagen en vivo de las **cámaras IP por RTSP** tiene menos retraso. Esto ayuda a que el video y el audio del salón coincidan mejor para quienes siguen la reunión por Zoom.
- En **Zoom para Windows**, puedes silenciar a todos los participantes o pedirles que activen el micrófono aunque el botón **Participantes** esté en el menú de opciones adicionales de la barra compacta.
- Los **paneles de la barra de herramientas** se mantienen abiertos cuando cambias a otra aplicación. Al hacer clic en otro control de Solin, ese mismo clic cierra el panel y activa el control.
- La **sincronización de carpetas vinculadas** gestiona mejor los cambios momentáneos en los archivos durante la configuración. Si hay un fallo temporal al guardar, Solin conserva los cambios de la reunión y vuelve a intentarlo. El contenido multimedia oficial restaurado sigue visible al recibir cambios anteriores de otro equipo.
- Los **controles de NDI** vuelven a aparecer en los ajustes de OBS de Solin, así que puedes elegir la señal que se va a proyectar.
- Solin vuelve a obtener automáticamente el **texto anual**.
