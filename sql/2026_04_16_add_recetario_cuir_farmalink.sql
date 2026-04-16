ALTER TABLE recetario_recetas
ADD COLUMN IF NOT EXISTS cuir VARCHAR(50);

ALTER TABLE recetario_recetas
ADD COLUMN IF NOT EXISTS sent_to_farmalink BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE recetario_recetas
ADD COLUMN IF NOT EXISTS farmalink_response JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS uq_recetario_recetas_cuir
ON recetario_recetas (cuir)
WHERE cuir IS NOT NULL;
