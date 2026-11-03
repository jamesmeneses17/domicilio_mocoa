CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE zonas (
  id SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL UNIQUE,
  tarifa_domicilio NUMERIC(10,2) NOT NULL
);

CREATE TABLE clientes (
  id SERIAL PRIMARY KEY,
  sesion_id TEXT UNIQUE,              -- identificador del navegador/sesión web
  nombre TEXT NOT NULL,
  telefono TEXT,
  direccion_habitual TEXT,
  zona_id INT REFERENCES zonas(id)
);

CREATE TABLE restaurantes (
  id SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL,
  abierto BOOLEAN NOT NULL DEFAULT TRUE,
  hora_apertura TIME NOT NULL,
  hora_cierre TIME NOT NULL,
  zona_id INT REFERENCES zonas(id)
);

CREATE TABLE productos (
  id SERIAL PRIMARY KEY,
  restaurante_id INT NOT NULL REFERENCES restaurantes(id),
  nombre TEXT NOT NULL,
  descripcion TEXT,
  precio NUMERIC(10,2) NOT NULL,
  disponible BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE domiciliarios (
  id SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL,
  zona_id INT REFERENCES zonas(id),
  estado TEXT NOT NULL DEFAULT 'libre'
    CHECK (estado IN ('libre','ocupado','fuera_de_servicio')),
  pedidos_activos INT NOT NULL DEFAULT 0
);

CREATE TABLE pedidos (
  id SERIAL PRIMARY KEY,
  cliente_id INT NOT NULL REFERENCES clientes(id),
  restaurante_id INT NOT NULL REFERENCES restaurantes(id),
  domiciliario_id INT REFERENCES domiciliarios(id),
  estado TEXT NOT NULL DEFAULT 'borrador'
    CHECK (estado IN ('borrador','programado','pendiente_restaurante','aceptado',
                      'rechazado','asignado','en_camino','entregado','cancelado')),
  direccion_entrega TEXT NOT NULL,
  zona_id INT REFERENCES zonas(id),
  metodo_pago TEXT,
  subtotal NUMERIC(10,2) NOT NULL DEFAULT 0,
  costo_domicilio NUMERIC(10,2) NOT NULL DEFAULT 0,
  total NUMERIC(10,2) NOT NULL DEFAULT 0,
  programado_para TIMESTAMPTZ,
  notas TEXT,
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE pedido_items (
  id SERIAL PRIMARY KEY,
  pedido_id INT NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
  producto_id INT NOT NULL REFERENCES productos(id),
  cantidad INT NOT NULL CHECK (cantidad > 0),
  precio_unitario NUMERIC(10,2) NOT NULL,
  observacion TEXT
);

-- Memoria de la conversación
CREATE TABLE mensajes (
  id SERIAL PRIMARY KEY,
  sesion_id TEXT NOT NULL,
  rol TEXT NOT NULL CHECK (rol IN ('usuario','agente','sistema')),
  contenido TEXT NOT NULL,
  canal TEXT NOT NULL DEFAULT 'texto' CHECK (canal IN ('texto','voz')),
  pedido_id INT REFERENCES pedidos(id),
  ms_respuesta INT,                   -- tiempo de respuesta (evaluación)
  audio_archivo TEXT,                 -- audio TTS asociado a la respuesta
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trazabilidad: cada herramienta que usa el agente
CREATE TABLE acciones_agente (
  id SERIAL PRIMARY KEY,
  sesion_id TEXT,
  pedido_id INT REFERENCES pedidos(id),
  herramienta TEXT NOT NULL,
  entrada JSONB,
  salida JSONB,
  ms INT,
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Errores (el profesor pide registrarlos)
CREATE TABLE errores (
  id SERIAL PRIMARY KEY,
  sesion_id TEXT,
  componente TEXT NOT NULL,           -- 'stt','tts','llm','herramienta','db'
  detalle TEXT NOT NULL,
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conocimiento semántico (embeddinggemma = 768 dimensiones)
CREATE TABLE conocimiento (
  id SERIAL PRIMARY KEY,
  restaurante_id INT REFERENCES restaurantes(id),
  tipo TEXT NOT NULL,                 -- 'politica','faq','menu','lugar'
  titulo TEXT,
  contenido TEXT NOT NULL,
  embedding vector(768)
);
CREATE INDEX ON conocimiento USING hnsw (embedding vector_cosine_ops);
