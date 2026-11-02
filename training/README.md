# Training

[`paysim_lightgbm.ipynb`](paysim_lightgbm.ipynb) trains a LightGBM fraud model on the
[PaySim](https://www.kaggle.com/datasets/ealaxi/paysim1) dataset, compares it with a rule and a logistic
regression, and saves the model and its metadata. It runs on Kaggle Notebooks or Google Colab and needs no GPU.

## Run it on Kaggle (recommended)

1. At <https://www.kaggle.com/code> create a new notebook, then **File > Import Notebook** and upload
   `paysim_lightgbm.ipynb`.
2. In the right-hand panel choose **Add Input**, search for `paysim1` ("Synthetic Financial Datasets For Fraud
   Detection" by ealaxi) and add it. No download or API key is needed.
3. Leave the accelerator on **None** (CPU); the model does not use a GPU. Switch **Internet** on in the settings
   only if the first cell says a library is missing.
4. Run the cells from the top. Keep `QUICK = True` for a first run of a few minutes, then set it to `False`.
5. Results go to `/kaggle/working`. Choose **Save Version > Save & Run All (Commit)** to keep them: they then
   appear in the notebook's **Output** tab, where you can download them or turn them into a Kaggle Dataset.

## Run it in Google Colab instead

Upload the notebook at <https://colab.research.google.com> (**File > Upload notebook**) and run it from the top.
When asked for Kaggle credentials, upload `kaggle.json` or store `KAGGLE_USERNAME` and `KAGGLE_KEY` in Colab's
Secrets panel. The last cell saves the results to your Google Drive (`MyDrive/fraudradar/run_<date>_<time>`,
controlled by `SAVE_TO_DRIVE`) and downloads a zip. Colab's own disk is wiped when the session ends, so do not
rely on it. If the download fails, put the CSV in Colab or Drive by hand and set `PAYSIM_CSV` to its path.

## Run it on your own machine

```bash
conda activate fraudradar-train
```

```bash
jupyter lab training/paysim_lightgbm.ipynb
```

Outputs go to `work/`, which is git-ignored. Do not commit datasets or trained models; link to the dataset instead.

## What the notebook decides for you (change them on purpose)

| Decision | Why |
|---|---|
| Split by time (60% / 20% / 20%) | The model is used on future payments. A random split would leak the future. |
| Only information from before each payment | The balances recorded after a payment give the answer away in PaySim. |
| Average precision and recall at a fixed false-alarm rate | Accuracy is meaningless at about 0.1% fraud. |
| Threshold chosen on the validation part | The test part must stay untouched until the end. |

## Reading the results honestly

PaySim is itself a simulation, built from a sample of real mobile-money logs. A good score here shows the
pipeline and the split are sound; it is **not** proof that the model would work on real fraud. Say that when you
present it, and consider repeating the exercise on IEEE-CIS, which is real data.
