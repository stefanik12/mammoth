# Modular Model

MAMMOTH is a modular framework for machine translation that offers flexibility in designing different sharing schemes for its modules. Let's break down the key aspects and features mentioned:

1. **Depth of Encoder & Decoder**:
    - **Balanced**: The encoder and decoder have the same depths.
    - **Deep-enc-shallow-dec**: The encoder is deep, while the decoder is relatively shallow.

2. **Target Language Selection Token**:
    - There is an option to include or exclude a target language selection token in the architecture.

3. **Layerwise Parameter Sharing Schemes**:
    - **Fully Shared Encoder and Fully Shared Decoder (Baseline)**: Both the encoder and decoder have shared parameters, meaning they are common across all languages or translation pairs.
    - **Fully Shared Encoder and Target-Specific Decoder**: The encoder is shared, but each target language has its own decoder.
    - **Apple-Style Learning Language-specific Layers**: The encoder contains both source and target-specific layers, which suggests a more intricate sharing pattern.
    - **Neural Architecture Search**: Potentially, a method to automatically discover optimal sharing patterns.
    - **Adapter-Like Low-Rank Residual Layers**: This implies the use of adapter-like layers with low-rank residual connections, which can facilitate adaptation for different languages or translation tasks.

4. **Groups for Groupwise Shared Parameters**:
    - **Not Used (Only Fully Shared and Language-Specific)**: In this case, there are no specific parameter sharing groups beyond fully shared and language-specific ones.
    - **Phylogenetic**: Parameters could be shared among languages that are phylogenetically related, meaning they share a common ancestry.
    - **Clustering Based on Typological Database**: Sharing could be determined based on linguistic typological features or characteristics.
    - **Clustering Based on Language Embeddings**: This could involve sharing parameters based on the embeddings of languages in a common vector space.

5. **Subword Vocabularies**:
    - **Fully Shared vs Language-Specific**: The system allows for a choice between using a fully shared subword vocabulary for all languages or using language-specific subword vocabularies, which can be beneficial for languages with unique linguistic characteristics.