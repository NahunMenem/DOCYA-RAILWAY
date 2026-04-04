-- DocYa
-- Migración para login Google + perfil global obligatorio de pacientes.

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tipo_documento TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS numero_documento TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS direccion TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS perfil_completo BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS acepta_terminos BOOLEAN DEFAULT FALSE;

-- `telefono`, `fecha_nacimiento` y `sexo` ya existen en esta base.
-- Si en algún entorno faltan, descomentá estas líneas:
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS telefono TEXT;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS fecha_nacimiento DATE;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS sexo TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id_unique
ON users (google_id)
WHERE google_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_perfil_completo
ON users (perfil_completo);

UPDATE users
SET perfil_completo = FALSE
WHERE perfil_completo IS NULL;
