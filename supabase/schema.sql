CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.usuarios (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canal_preferido text,
    identificador_externo text,
    nombre text,
    correo text,
    creado_en timestamptz NOT NULL DEFAULT now(),
    UNIQUE (canal_preferido, identificador_externo)
);

CREATE TABLE IF NOT EXISTS public.usuarios_panel (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE
        CHECK (
            username = lower(username)
            AND length(username) BETWEEN 3 AND 64
            AND username ~ '^[a-z0-9._-]+$'
        ),
    nombre text NOT NULL,
    password_hash text NOT NULL,
    activo boolean NOT NULL DEFAULT true,
    creado_por text NOT NULL,
    creado_en timestamptz NOT NULL DEFAULT now(),
    ultimo_acceso_en timestamptz
);

CREATE TABLE IF NOT EXISTS public.canales (
    id bigserial PRIMARY KEY,
    codigo text NOT NULL UNIQUE,
    nombre text NOT NULL,
    activo boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS public.agentes (
    id bigserial PRIMARY KEY,
    codigo text NOT NULL UNIQUE,
    nombre text NOT NULL,
    nivel integer NOT NULL CHECK (nivel BETWEEN 1 AND 4),
    descripcion text,
    activo boolean NOT NULL DEFAULT true
);

ALTER TABLE public.agentes
    DROP CONSTRAINT IF EXISTS agentes_nivel_check;
ALTER TABLE public.agentes
    ADD CONSTRAINT agentes_nivel_check CHECK (nivel BETWEEN 1 AND 4);

CREATE TABLE IF NOT EXISTS public.casos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canal text NOT NULL CHECK (canal IN ('TELEGRAM', 'GMAIL', 'DISCORD', 'WEB')),
    remitente_id text,
    remitente_nombre text,
    conversacion_id text,
    asunto text,
    mensaje text NOT NULL,
    categoria text NOT NULL DEFAULT 'GENERAL',
    prioridad text NOT NULL DEFAULT 'NORMAL'
        CHECK (prioridad IN ('BAJA', 'NORMAL', 'ALTA', 'CRITICA')),
    agente_asignado text NOT NULL DEFAULT 'RESOLUTOR',
    estado text NOT NULL DEFAULT 'RECIBIDO',
    respuesta text,
    mensaje_externo_id text,
    recibido_en timestamptz NOT NULL DEFAULT now(),
    actualizado_en timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.casos
        WHERE mensaje_externo_id IS NOT NULL
        GROUP BY canal, mensaje_externo_id
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'No se puede crear idx_casos_canal_mensaje_externo: hay casos duplicados por canal y mensaje_externo_id.';
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_casos_canal_mensaje_externo
    ON public.casos (canal, mensaje_externo_id);

CREATE TABLE IF NOT EXISTS public.mensajes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    caso_id uuid REFERENCES public.casos(id) ON DELETE CASCADE,
    direccion text NOT NULL CHECK (direccion IN ('ENTRADA', 'SALIDA')),
    canal text NOT NULL,
    contenido text NOT NULL,
    id_externo text,
    creado_en timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ejecuciones_agentes (
    id bigserial PRIMARY KEY,
    caso_id uuid REFERENCES public.casos(id) ON DELETE CASCADE,
    agente_codigo text NOT NULL,
    entrada jsonb,
    salida jsonb,
    estado text NOT NULL DEFAULT 'OK',
    error text,
    inicio_en timestamptz NOT NULL DEFAULT now(),
    fin_en timestamptz
);

CREATE TABLE IF NOT EXISTS public.eventos_caso (
    id bigserial PRIMARY KEY,
    caso_id uuid REFERENCES public.casos(id) ON DELETE CASCADE,
    evento text NOT NULL,
    detalle jsonb,
    creado_en timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.canales (codigo, nombre)
VALUES
    ('TELEGRAM', 'Telegram'),
    ('GMAIL', 'Gmail'),
    ('DISCORD', 'Discord')
ON CONFLICT (codigo) DO NOTHING;

DELETE FROM public.canales WHERE codigo = 'WEB';

INSERT INTO public.agentes (codigo, nombre, nivel, descripcion)
VALUES
    ('CLASIFICADOR', 'Clasificador', 1, 'Clasifica solicitudes'),
    ('RESOLUTOR', 'Resolutor', 1, 'Resuelve consultas frecuentes'),
    ('ESPECIALISTA', 'Especialista', 2, 'Atiende incidencias escaladas'),
    ('SUPERVISOR', 'Supervisor', 3, 'Supervisa casos y calidad'),
    ('ESPECIALIZADO', 'Unidad especializada', 4, 'Atiende casos de nivel especializado')
ON CONFLICT (codigo) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_casos_recibido_en
    ON public.casos (recibido_en DESC);

CREATE INDEX IF NOT EXISTS idx_casos_canal_estado
    ON public.casos (canal, estado);

CREATE INDEX IF NOT EXISTS idx_mensajes_caso_creado
    ON public.mensajes (caso_id, creado_en);

CREATE INDEX IF NOT EXISTS idx_ejecuciones_caso_inicio
    ON public.ejecuciones_agentes (caso_id, inicio_en);

CREATE INDEX IF NOT EXISTS idx_eventos_caso_creado
    ON public.eventos_caso (caso_id, creado_en);
