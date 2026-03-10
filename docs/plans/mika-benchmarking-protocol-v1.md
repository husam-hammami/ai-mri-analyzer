# Mika Benchmarking & Validation Protocol (v1.0)

## Objective
Establish a reproducible, automated benchmarking pipeline to evaluate the diagnostic and quantitative accuracy of the Mika (`ai-mri-analyzer`) pipeline against established, multi-modal clinical ground truths. 

## Datasets
We will utilize two standard open-source medical imaging datasets to test different failure modes of the VLM architecture:

1. **fastMRI (NYU Langone / Meta)**
   * **Target:** Structural integrity and common pathologies (e.g., meniscal tears in knees, neuro anomalies).
   * **Format:** Raw k-space and DICOM.
   * **Utility:** Tests the system's baseline ability to identify standard anatomical abnormalities without contrast.

2. **BraTS (Brain Tumor Segmentation)**
   * **Target:** Multi-sequence spatial correlation (T1, T1Gd, T2, FLAIR).
   * **Format:** NIfTI (requires translation layer for Mika's pipeline).
   * **Utility:** Stresses Mika's (and Claude's) ability to cross-reference sequences to identify subtle volumetric anomalies and edema boundaries.



## Evaluation Metrics

Since Mika outputs unstructured clinical text (LLM generated) rather than strict segmentation masks, validation requires natural language processing (NLP) to extract claimed findings and map them against the dataset's ground truth annotations.

### 1. Classification Metrics (Pathology Detection)
For binary or multi-class classification (e.g., "Tumor Present" vs. "Normal"):

* **Precision (Positive Predictive Value):** Measures the hallucination rate. 
  $$Precision = \frac{TP}{TP + FP}$$
* **Recall (Sensitivity):** Measures the rate of missed diagnoses (false negatives).
  $$Recall = \frac{TP}{TP + FN}$$
* **F1-Score:** The harmonic mean of precision and recall.
  $$F1 = 2 \times \frac{Precision \times Recall}{Precision + Recall}$$

### 2. Quantitative Metrics (Deterministic Layer)
For the deterministic DICOM metadata layer (PixelSpacing measurements):

* **Mean Absolute Error (MAE):** Compare Mika's calculated distances (e.g., AP diameter) against ground truth manual annotations.
* **Tolerance Threshold:** Pass/Fail boolean if the MAE is $\leq 1.5\text{mm}$. 

## Execution Pipeline

1. **Data Ingestion:** Script to sample 100 random multi-sequence studies from BraTS and 100 from fastMRI.
2. **Translation Layer:** Convert NIfTI/Raw arrays into standard DICOM/PNG pairs to match Mika's current ingestion expectations.
3. **Automated Inference:** Batch run the Mika orchestration script. Log all LLM output reports to a JSON structure.
4. **Entity Extraction (LLM-as-a-Judge):** Use a strict, low-temperature LLM prompt to parse Mika's generated reports into structured JSON arrays of findings (e.g., `["T2_hyperintensity", "midline_shift"]`).
5. **Scoring:** Compare the extracted entities against the verified dataset ground truth and calculate Precision, Recall, and F1.

## Success Criteria for V1
* **Metadata Calculations:** 99%+ Accuracy ($\leq 1.5\text{mm}$ MAE).
* **Gross Pathology Flagging:** Recall $\ge 80\%$, Precision $\ge 50\%$ (Acknowledge high false-positive rate as baseline).