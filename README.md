# ews_autoreply

Script en Python con **interfaz mínima (Tkinter)** para responder correos automáticamente cuando estás de vacaciones, usando Exchange vía `exchangelib`.

## ¿Qué hace?

- Se conecta a un buzón de Exchange (EWS).
- Revisa la bandeja de entrada en intervalos configurables.
- Envía una respuesta automática con asunto/cuerpo personalizable.
- Evita loops y duplicados con controles de seguridad:
  - no responde a autorespuestas típicas,
  - evita responder más de una vez al mismo mensaje,
  - limita respuestas por hora,
  - aplica ventana de tiempo por remitente.
- Responde tanto remitentes internos como externos; solo saltea exclusiones configuradas y detección de autorespuestas.
- Guarda trazabilidad en SQLite (`autoreply.db`) y muestra logs en pantalla.
- Permite guardar/cargar configuración en JSON.

## Requisitos

- Python 3.11
- Conda (recomendado para crear entorno)
- Dependencia Python:
  - `exchangelib`

## Instalación rápida

```bash
conda create -n ews_autoreply python=3.11 -y
conda activate ews_autoreply
pip install exchangelib
```

## Variable de entorno para la contraseña

El script toma la contraseña desde la variable `EWS_MAIL_PASSWORD`.

En **Windows** (recomendado):

```bat
setx EWS_MAIL_PASSWORD "TU_CLAVE"
```

> Cerrá y abrí nuevamente la terminal después de `setx` para que tome la variable.
>
> Si la clave quedó guardada con comillas, la app ahora las limpia automáticamente (por ejemplo `"mi_clave"` o `'mi_clave'`).

## Ejecución

Desde la carpeta del proyecto:

```bash
python ews_autoreply.py
```

Se abrirá una ventana con los campos principales:

- `Email`
- `Server`
- `Auth Type` (por defecto `NTLM`)
- `Start date`
- `Poll (s)`
- `Recent window (min)`
- `Max replies/hour`
- `Subject reply`
- `Body` del mensaje automático
- listas de exclusión (`exclude_emails`, `exclude_domains`)

Luego:

1. Completá/ajustá configuración.
2. Presioná **Iniciar**.
3. Para detener, presioná **Detener**.

## Configuración recomendada para “vacaciones”

- Personalizá `subject_reply` y `generic_reply_body` con un texto de ausencia.
- Definí un contacto alternativo en el cuerpo del mensaje.
- Ajustá `recent_window_minutes` para no responder repetidamente al mismo remitente.
- Cargá dominios o correos a excluir si no querés auto responder internamente o a ciertos remitentes.

Ejemplo de mensaje de vacaciones:

```text
Hola,

Gracias por tu mensaje. Actualmente estoy de vacaciones y no tendré acceso regular a esta casilla.

Si tu consulta es urgente, por favor contactá a un reemplazo del equipo.

Saludos.
```

## Persistencia y archivos

- Base de datos local: `autoreply.db`
- Config opcional auto-cargable: `config.json` en el mismo directorio
- También podés guardar/cargar configuración manualmente desde la interfaz

## Notas importantes

- Si falta `EWS_MAIL_PASSWORD`, la app no inicia el motor de respuestas.
- Revisá `server`, `email` y `auth_type` según tu infraestructura Exchange.
- Probalo primero con una casilla de prueba antes de usarlo en producción.

## Idea del proyecto

La idea es mantener un script simple con interfaz mínima que contesta correos de manera automática para informar que estás de vacaciones, con trazabilidad local y controles básicos anti-loop.
