INSERT INTO zonas (nombre, tarifa_domicilio) VALUES
  ('Centro', 3000), ('Pablo VI', 4500), ('Condagua', 6000);

INSERT INTO restaurantes (nombre, hora_apertura, hora_cierre, zona_id) VALUES
  ('Corrientazo Doña Rosa', '11:00', '15:00', 1),
  ('Hamburguesas El Puente', '12:00', '22:00', 1);

INSERT INTO productos (restaurante_id, nombre, descripcion, precio) VALUES
  (1, 'Corrientazo de pollo', 'Sopa, arroz, pollo, ensalada y jugo', 14000),
  (1, 'Corrientazo de carne', 'Sopa, arroz, carne, ensalada y jugo', 15000),
  (1, 'Limonada', 'Limonada natural 12 oz', 3500),
  (2, 'Hamburguesa clásica', 'Carne, queso, lechuga, tomate y cebolla', 16000),
  (2, 'Hamburguesa doble', 'Doble carne, queso y tocineta', 22000),
  (2, 'Gaseosa', 'Gaseosa 400 ml', 3000);

-- Se añade un domiciliario en Condagua para que esa zona tenga cobertura.
INSERT INTO domiciliarios (nombre, zona_id) VALUES
  ('Pedro', 1), ('Luis', 2), ('Andrés', 1), ('Marta', 3);
