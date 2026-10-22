"""Domain vocabulary used for query expansion and request parsing.

Everything here is static, curated and auditable. Query expansion is
deterministic: it never invents sources, only alternate terminology.
"""

from __future__ import annotations

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "of", "for", "to", "in", "on",
    "at", "by", "with", "about", "into", "over", "after", "before", "between", "from", "up",
    "down", "out", "off", "is", "are", "was", "were", "be", "been", "being", "do", "does",
    "did", "have", "has", "had", "i", "we", "you", "they", "it", "this", "that", "these",
    "those", "me", "my", "our", "your", "their", "as", "so", "such", "can", "could", "should",
    "would", "may", "might", "must", "will", "shall", "not", "no", "any", "all", "some",
    "more", "most", "other", "another", "find", "search", "look", "looking", "get", "give",
    "show", "list", "need", "want", "help", "please", "using", "use", "used", "via", "like",
    "related", "relevant", "available", "public", "publicly", "open", "dataset", "datasets",
    "data", "paper", "papers", "research", "study", "studies", "article", "articles",
    "publication", "publications", "source", "sources", "good", "best", "reliable",
    "machine", "learning", "model", "models", "training", "evaluating", "evaluation",
    "existing", "identify", "detect", "detecting", "predict", "predicting", "prediction",
    "worldwide", "global", "across", "countries", "country", "many", "much", "very",
}

# Domain detection lexicon: domain label -> trigger terms.
DOMAIN_TERMS: dict[str, tuple[str, ...]] = {
    "finance": ("financial", "fraud", "banking", "credit", "transaction", "stock", "market",
                "insurance", "fintech", "payment", "loan", "investment", "cryptocurrency"),
    "healthcare": ("medical", "clinical", "health", "patient", "diagnosis", "disease", "cancer",
                   "hospital", "ehr", "biomedical", "drug", "epidemiology", "trial"),
    "environment": ("climate", "air pollution", "pollution", "emission", "weather", "temperature",
                    "carbon", "biodiversity", "ecosystem", "water quality", "land use", "wildfire"),
    "energy": ("battery", "energy", "solar", "wind", "grid", "power", "photovoltaic",
               "degradation", "fuel", "hydrogen", "electric vehicle"),
    "geoscience": ("geospatial", "satellite", "remote sensing", "seismic", "geology", "earthquake",
                   "ocean", "hydrology", "topography", "gis"),
    "nlp": ("natural language", "text classification", "sentiment", "translation", "language model",
            "nlp", "corpus", "question answering", "summarization", "named entity", "speech"),
    "vision": ("image", "computer vision", "object detection", "segmentation", "vision",
               "classification of images", "medical imaging", "video"),
    "graph": ("graph neural", "gnn", "knowledge graph", "network analysis", "graph learning",
              "node classification", "link prediction"),
    "education": ("education", "student", "learning outcome", "school", "curriculum", "mooc"),
    "agriculture": ("agriculture", "crop", "soil", "yield", "livestock", "farming", "irrigation"),
    "transport": ("traffic", "transport", "vehicle", "mobility", "transit", "autonomous driving"),
    "social": ("social media", "sentiment", "demographic", "survey", "behavior", "opinion"),
    "security": ("malware", "intrusion", "cybersecurity", "phishing", "vulnerability", "attack"),
    "astronomy": ("astronomy", "galaxy", "stellar", "exoplanet", "cosmology", "telescope"),
    "biology": ("genomic", "gene", "protein", "sequencing", "microbiome", "crispr", "rna"),
}

MODALITY_TERMS: dict[str, tuple[str, ...]] = {
    "tabular": ("tabular", "csv", "spreadsheet", "structured", "transaction", "records"),
    "text": ("text", "corpus", "documents", "reviews", "tweets", "abstracts", "nlp"),
    "image": ("image", "images", "photograph", "radiograph", "x-ray", "satellite imagery",
              "ct scan", "mri", "picture", "visual"),
    "audio": ("audio", "speech", "sound", "voice", "acoustic"),
    "video": ("video", "footage", "clip", "stream"),
    "timeseries": ("time series", "timeseries", "temporal", "sensor readings", "sequence of",
                   "degradation over", "forecasting"),
    "graph": ("graph", "network", "nodes", "edges"),
    "geospatial": ("geospatial", "geo", "coordinates", "shapefile", "raster", "map"),
    "multimodal": ("multimodal", "multi-modal", "image-text", "vision-language"),
}

# Terminology expansions used to broaden a search without changing its meaning.
SYNONYM_MAP: dict[str, tuple[str, ...]] = {
    "fraud": ("fraud detection", "anomaly detection", "financial crime"),
    "fraudulent": ("fraud", "anomaly"),
    "medical image": ("medical imaging", "radiomics", "clinical imaging"),
    "battery": ("battery degradation", "state of health", "li-ion"),
    "degradation": ("aging", "capacity fade", "degradation prediction"),
    "air pollution": ("air quality", "particulate matter", "PM2.5", "atmospheric pollution"),
    "research gap": ("open problems", "unaddressed challenges", "future work"),
    "graph neural network": ("GNN", "graph representation learning", "message passing"),
    "machine learning": ("statistical learning", "supervised learning"),
    "energy": ("power", "electricity"),
    "climate": ("climate change", "global warming"),
    "sentiment": ("opinion mining", "emotion detection"),
    "network intrusion": ("intrusion detection system", "IDS", "network security"),
    "clinical": ("medical", "health"),
    "geospatial": ("spatial", "GIS"),
    "benchmark": ("evaluation benchmark", "leaderboard"),
    "time series": ("temporal sequence", "sequential data"),
    "tabular": ("structured", "relational"),
    "prediction": ("forecasting", "estimation"),
    "detection": ("identification", "recognition"),
    "classification": ("categorisation", "categorization"),
    "annotation": ("labelling", "labeling", "ground truth"),
    "node classification": ("node labelling", "graph classification"),
    "remote sensing": ("earth observation", "satellite imagery"),
    "genomic": ("genomics", "genome sequencing"),
    "protein": ("proteomics", "protein structure"),
    "cybersecurity": ("computer security", "information security"),
    "supply chain": ("logistics", "procurement"),
}

# Query templates applied per intent.
LITERATURE_TEMPLATES = (
    "{topic}",
    "{topic} systematic review",
    "{topic} survey",
    "{topic} benchmark evaluation",
    "{topic} limitations future work",
    "{topic} reproducibility",
)

DATASET_TEMPLATES = (
    "{topic} dataset",
    "{topic} benchmark dataset",
    "{topic} open data",
    "{topic} corpus",
    "{topic} annotated data",
    "{topic} repository",
)

# Words that indicate the user primarily wants datasets or literature.
DATASET_TRIGGERS = (
    "dataset", "datasets", "data set", "corpus", "benchmark", "training data", "open data",
    "data repository", "downloaded", "download", "csv", "collection of data",
)
LITERATURE_TRIGGERS = (
    "papers", "paper", "literature", "studies", "research", "publications", "articles",
    "review", "state of the art", "gaps", "evidence", "citations",
)
ACCESS_TRIGGERS = {
    "open": ("open access", "openly available", "publicly available", "free", "no paywall",
             "permissively licensed", "cc0", "cc-by"),
    "restricted": ("licensed", "restricted", "requires application", "request access", "gated"),
}
GEOGRAPHY_TERMS = (
    "africa", "asia", "europe", "north america", "south america", "latin america", "india",
    "china", "united states", "usa", "uk", "united kingdom", "germany", "france", "japan",
    "brazil", "canada", "australia", "russia", "indonesia", "nigeria", "kenya", "mexico",
    "worldwide", "global", "developing countries", "eu", "southeast asia", "middle east",
)
