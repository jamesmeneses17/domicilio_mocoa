-- Datos de restaurantes reales (importados del Word «Menus_Restaurantes_Mocoa») y ocultamiento de los de demo.
-- Idempotente.  Base existente:  docker exec -i domicilios_db psql -U domicilios < db/init/04_restaurantes_reales.sql

ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS activo      BOOLEAN NOT NULL DEFAULT true;  -- false = oculto para el agente
ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS tipo_comida TEXT;
ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS telefono    TEXT;
ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS direccion   TEXT;
ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS notas       TEXT;
ALTER TABLE restaurantes ADD COLUMN IF NOT EXISTS fuente      TEXT;                            -- 'demo' | 'menu_mocoa'

-- Los restaurantes que ya existían eran datos de demostración: se ocultan (no se borran).
UPDATE restaurantes SET activo = false, fuente = 'demo' WHERE fuente IS NULL;

-- Necesario para que el importador pueda actualizar en lugar de duplicar.
CREATE UNIQUE INDEX IF NOT EXISTS restaurantes_nombre_uq ON restaurantes (nombre);
CREATE UNIQUE INDEX IF NOT EXISTS productos_restaurante_nombre_uq ON productos (restaurante_id, nombre);
