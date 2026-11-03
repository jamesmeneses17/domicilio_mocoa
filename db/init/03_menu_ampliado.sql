-- Menú ampliado con atributos para recomendar según el clima y el antojo.
-- Es idempotente: se puede ejecutar varias veces sin duplicar nada.
--   Instalación nueva: Docker lo ejecuta solo (después de 01 y 02).
--   Base existente:    docker exec -i domicilios_db psql -U domicilios < db/init/03_menu_ampliado.sql

ALTER TABLE productos ADD COLUMN IF NOT EXISTS categoria   TEXT;                    -- plato fuerte, sopa, bebida, postre, snack
ALTER TABLE productos ADD COLUMN IF NOT EXISTS temperatura TEXT;                    -- caliente | fria
ALTER TABLE productos ADD COLUMN IF NOT EXISTS etiquetas   TEXT[] NOT NULL DEFAULT '{}';
-- etiquetas: liviano, contundente, economico, dulce, refrescante, para_el_frio, picante, sin_picante,
--            vegetariano, saludable, desayuno, para_compartir

-- Atributos de los productos que ya existían
UPDATE productos SET categoria='plato fuerte', temperatura='caliente', etiquetas='{contundente,economico,sin_picante}' WHERE nombre IN ('Corrientazo de pollo','Corrientazo de carne');
UPDATE productos SET categoria='bebida', temperatura='fria', etiquetas='{refrescante,economico,dulce}' WHERE nombre='Limonada';
UPDATE productos SET categoria='plato fuerte', temperatura='caliente', etiquetas='{contundente,sin_picante}' WHERE nombre='Hamburguesa clásica';
UPDATE productos SET categoria='plato fuerte', temperatura='caliente', etiquetas='{contundente,para_compartir,sin_picante}' WHERE nombre='Hamburguesa doble';
UPDATE productos SET categoria='bebida', temperatura='fria', etiquetas='{refrescante,economico,dulce}' WHERE nombre='Gaseosa';

-- Restaurantes nuevos
INSERT INTO restaurantes (nombre, hora_apertura, hora_cierre, zona_id)
SELECT v.nombre, v.ap::time, v.ci::time, 1
FROM (VALUES ('Sopas y Café La Montaña','06:00','21:00'),
             ('Helados y Jugos Amazonía','10:00','21:00')) AS v(nombre, ap, ci)
WHERE NOT EXISTS (SELECT 1 FROM restaurantes r WHERE r.nombre = v.nombre);

-- Productos nuevos (restaurante, nombre, descripción, precio, categoría, temperatura, etiquetas)
INSERT INTO productos (restaurante_id, nombre, descripcion, precio, categoria, temperatura, etiquetas)
SELECT r.id, v.nombre, v.descripcion, v.precio, v.categoria, v.temperatura, v.etiquetas::text[]
FROM (VALUES
  -- Sopas y Café La Montaña
  ('Sopas y Café La Montaña','Ajiaco santafereño','Sopa espesa de pollo, tres clases de papa y mazorca, con crema y alcaparras',16000,'sopa','caliente','{contundente,para_el_frio,sin_picante}'),
  ('Sopas y Café La Montaña','Sancocho de gallina','Sopa tradicional con gallina, yuca, plátano y mazorca',18000,'sopa','caliente','{contundente,para_el_frio}'),
  ('Sopas y Café La Montaña','Caldo de costilla','Caldo reconfortante de costilla con papa y cilantro, ideal para la mañana',9000,'sopa','caliente','{economico,liviano,desayuno,para_el_frio}'),
  ('Sopas y Café La Montaña','Chocolate caliente con queso','Chocolate espeso servido con queso y pan',6500,'bebida','caliente','{dulce,para_el_frio,desayuno}'),
  ('Sopas y Café La Montaña','Café con leche','Café colombiano con leche caliente',3500,'bebida','caliente','{economico,desayuno,para_el_frio}'),
  ('Sopas y Café La Montaña','Aguapanela caliente con limón','Aguapanela caliente con limón, reconfortante y ligera',3000,'bebida','caliente','{economico,liviano,para_el_frio,dulce}'),
  ('Sopas y Café La Montaña','Empanadas de pipián','Tres empanadas de pipián con ají suave',7000,'snack','caliente','{economico,sin_picante,para_compartir}'),
  -- Helados y Jugos Amazonía
  ('Helados y Jugos Amazonía','Helado de copoazú','Dos bolas de helado de copoazú, fruta amazónica',7000,'postre','fria','{dulce,refrescante,liviano}'),
  ('Helados y Jugos Amazonía','Jugo de lulo','Jugo natural de lulo bien frío',5000,'bebida','fria','{refrescante,liviano,economico}'),
  ('Helados y Jugos Amazonía','Jugo de maracuyá','Jugo natural de maracuyá bien frío',5000,'bebida','fria','{refrescante,liviano,economico}'),
  ('Helados y Jugos Amazonía','Jugo de arazá','Jugo natural de arazá, fruta amazónica de sabor intenso',5500,'bebida','fria','{refrescante,liviano}'),
  ('Helados y Jugos Amazonía','Limonada de coco','Limonada cremosa con coco, muy fresca',6500,'bebida','fria','{refrescante,dulce}'),
  ('Helados y Jugos Amazonía','Ensalada de frutas con helado','Frutas frescas de la región con una bola de helado',9000,'postre','fria','{liviano,dulce,refrescante,vegetariano,saludable}'),
  ('Helados y Jugos Amazonía','Ensalada fresca de pollo','Pollo desmechado, lechuga, tomate y aguacate con limón',15000,'plato fuerte','fria','{liviano,saludable,sin_picante}'),
  -- Hamburguesas El Puente (extras)
  ('Hamburguesas El Puente','Hamburguesa vegetariana','Hamburguesa de lentejas con queso, lechuga y tomate',17000,'plato fuerte','caliente','{liviano,vegetariano,saludable,sin_picante}'),
  ('Hamburguesas El Puente','Papas fritas','Porción grande de papas fritas para compartir',6000,'snack','caliente','{economico,para_compartir,sin_picante}'),
  ('Hamburguesas El Puente','Malteada de vainilla','Malteada cremosa de vainilla',9000,'postre','fria','{dulce,refrescante}')
) AS v(restaurante, nombre, descripcion, precio, categoria, temperatura, etiquetas)
JOIN restaurantes r ON r.nombre = v.restaurante
WHERE NOT EXISTS (SELECT 1 FROM productos p WHERE p.restaurante_id = r.id AND p.nombre = v.nombre);
