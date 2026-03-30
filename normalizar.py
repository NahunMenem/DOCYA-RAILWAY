"""
PASO 1: Normalizar medicamentos_argentina.json → meds_clean.json
Parsea nombre comercial, forma farmacéutica, concentración y laboratorio.
"""
import json
import re

# ─── Formas farmacéuticas conocidas (en orden de prioridad) ──────────────────
FORMAS = [
    ("Comp. recub. con película", "Comprimido recubierto"),
    ("Comp. recub.",              "Comprimido recubierto"),
    ("Comp. efervescente",        "Comprimido efervescente"),
    ("Comp. bucodispersable",     "Comprimido bucodispersable"),
    ("Comp. masticable",          "Comprimido masticable"),
    ("Comp. sublingual",          "Comprimido sublingual"),
    ("Comp.",                     "Comprimido"),
    ("Cáps. dura",                "Cápsula dura"),
    ("Cáps. blanda",              "Cápsula blanda"),
    ("Cáps.",                     "Cápsula"),
    ("Sol. iny.",                 "Solución inyectable"),
    ("Sol. oral",                 "Solución oral"),
    ("Sol. oft.",                 "Solución oftálmica"),
    ("Sol.",                      "Solución"),
    ("Susp. oral",                "Suspensión oral"),
    ("Susp. iny.",                "Suspensión inyectable"),
    ("Susp.",                     "Suspensión"),
    ("Grag.",                     "Gragea"),
    ("Gran.",                     "Granulado"),
    ("Pom.",                      "Pomada"),
    ("Crema",                     "Crema"),
    ("Gel",                       "Gel"),
    ("Parche",                    "Parche"),
    ("Jarabe",                    "Jarabe"),
    ("Gotas",                     "Gotas"),
    ("Spray",                     "Spray"),
    ("Inhaler",                   "Inhalador"),
    ("Polvo",                     "Polvo"),
    ("Supositorio",               "Supositorio"),
    ("Óvulo",                     "Óvulo"),
]

# ─── Medicamentos sin receta (OTC) ────────────────────────────────────────────
SIN_RECETA = {
    "paracetamol", "ibuprofeno", "aspirina", "ácido acetilsalicílico",
    "loratadina", "cetirizina", "antazolina", "dextrometorfano",
    "ranitidina", "omeprazol", "pantoprazol", "loperamida",
    "metoclopramida", "domperidona", "bismuto", "simethicona",
    "hierro", "vitamina", "calcio", "magnesio", "zinc", "acido folico",
    "antiflamatorio", "clotrimazol", "miconazol",
}

# ─── Categorías por principio activo ─────────────────────────────────────────
CATEGORIAS = {
    "paracetamol": "Analgésico / Antipirético",
    "ibuprofeno": "AINE",
    "aspirina": "AINE / Antiagregante",
    "ácido acetilsalicílico": "AINE / Antiagregante",
    "amoxicilina": "Antibiótico",
    "azitromicina": "Antibiótico",
    "ciprofloxacina": "Antibiótico",
    "metformina": "Antidiabético",
    "atorvastatina": "Estatina",
    "losartán": "Antihipertensivo",
    "enalapril": "Antihipertensivo",
    "amlodipina": "Antihipertensivo",
    "omeprazol": "Inhibidor bomba de protones",
    "pantoprazol": "Inhibidor bomba de protones",
    "loratadina": "Antihistamínico",
    "cetirizina": "Antihistamínico",
    "alprazolam": "Ansiolítico",
    "diazepam": "Ansiolítico",
    "sertralina": "Antidepresivo",
    "fluoxetina": "Antidepresivo",
    "levotiroxina": "Hormona tiroidea",
    "insulina": "Insulina",
    "salbutamol": "Broncodilatador",
    "abacavir": "Antirretroviral",
    "lamivudina": "Antirretroviral",
    "aripiprazol": "Antipsicótico",
    "clonazepam": "Anticonvulsivante / Ansiolítico",
    "metotrexato": "Inmunosupresor",
    "prednisona": "Corticosteroide",
    "dexametasona": "Corticosteroide",
}


def extraer_forma(nombre: str):
    """Extrae la forma farmacéutica del nombre comercial."""
    for abrev, forma_larga in FORMAS:
        if abrev.lower() in nombre.lower():
            return forma_larga
    return None


def extraer_concentracion(nombre: str):
    """Extrae concentración tipo '500 mg', '1 g', '10%', '600 mg/300 mg'."""
    patron = r"(\d+(?:[,\.]\d+)?(?:\s*/\s*\d+(?:[,\.]\d+)?)?\s*(?:mg|g|mcg|µg|UI|%|ml|mg/ml))"
    matches = re.findall(patron, nombre, re.IGNORECASE)
    return matches[0].strip() if matches else None


def extraer_laboratorio(nombre: str):
    """El laboratorio suele estar entre el nombre genérico y la forma."""
    # Patron: NOMBRE_GENERICO LABORATORIO Comp./Sol./etc.
    for abrev, _ in FORMAS:
        idx = nombre.lower().find(abrev.lower())
        if idx > 0:
            parte = nombre[:idx].strip()
            palabras = parte.split()
            if len(palabras) >= 2:
                # Todo en mayúsculas después del primer token es el laboratorio
                return " ".join(palabras[1:]) if len(palabras) > 1 else None
    return None


def es_sin_receta(principios: list) -> bool:
    """Determina si el medicamento es OTC basado en principios activos."""
    principios_lower = [p.lower() for p in principios]
    return any(
        any(otc in p for otc in SIN_RECETA)
        for p in principios_lower
    )


def obtener_categoria(principios: list) -> str:
    """Devuelve la categoría terapéutica del medicamento."""
    for p in principios:
        p_lower = p.lower()
        for pa, cat in CATEGORIAS.items():
            if pa in p_lower:
                return cat
    return "Otro"


def normalizar(entry: dict, idx: int) -> dict:
    nombre_raw = entry.get("nombre", "")
    principios = entry.get("principio_activo", [])

    # principio_activo puede ser lista o string
    if isinstance(principios, str):
        principios = [p.strip() for p in principios.split("/")]
    elif not isinstance(principios, list):
        principios = []

    forma = extraer_forma(nombre_raw)
    concentracion = extraer_concentracion(nombre_raw)
    laboratorio = extraer_laboratorio(nombre_raw)

    # Nombre comercial: todo antes de la forma farmacéutica
    nombre_comercial = nombre_raw
    if forma:
        for abrev, _ in FORMAS:
            idx_forma = nombre_raw.lower().find(abrev.lower())
            if idx_forma > 0:
                nombre_comercial = nombre_raw[:idx_forma].strip()
                break

    return {
        "id": idx,
        "nombre_comercial": nombre_comercial,
        "nombre_completo": nombre_raw,
        "principio_activo": principios,
        "principio_activo_str": " / ".join(principios),  # para búsqueda de texto
        "laboratorio": laboratorio,
        "forma": forma,
        "concentracion": concentracion,
        "requiere_receta": not es_sin_receta(principios),
        "categoria": obtener_categoria(principios),
        "alertas": entry.get("alertas_composicion", []),
        "envases": entry.get("envases", []),
        # Campos para el médico (receta)
        "dosis_sugeridas": [],   # se puede poblar manualmente después
        "frecuencias_sugeridas": [],
    }


def main():
    print("Cargando medicamentos_argentina.json ...")
    with open("medicamentos_argentina.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Total: {len(data)} medicamentos. Normalizando ...")
    meds_clean = [normalizar(m, i) for i, m in enumerate(data)]

    # Stats
    con_forma = sum(1 for m in meds_clean if m["forma"])
    con_conc  = sum(1 for m in meds_clean if m["concentracion"])
    sin_receta = sum(1 for m in meds_clean if not m["requiere_receta"])

    print(f"  Con forma detectada:        {con_forma}/{len(meds_clean)}")
    print(f"  Con concentración detectada: {con_conc}/{len(meds_clean)}")
    print(f"  Sin receta (OTC):            {sin_receta}/{len(meds_clean)}")

    with open("meds_clean.json", "w", encoding="utf-8") as f:
        json.dump(meds_clean, f, ensure_ascii=False, indent=2)

    print(f"\nOK: meds_clean.json generado ({len(meds_clean)} medicamentos)")


if __name__ == "__main__":
    main()
