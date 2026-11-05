# Light-ASD model core

Source: https://github.com/Junhua-Liao/Light-ASD

Upstream commit: `ed38c232de5efe0261dbd68627c0ade7cdfe14eb`.

`Encoder.py`, `Classifier.py` and `Model.py` are the upstream model core. Imports in `Model.py` were changed to package-relative imports. The original MIT license is retained in `LICENSE`.

The upstream `weight/finetuning_TalkSet.model` is stored at `assets/models/light_asd_talkset.model`. Inference is implemented locally without the upstream training, shell-command or CUDA-only demo wrappers.

Reference: Junhua Liao et al., A Light Weight Model for Active Speaker Detection, CVPR 2023, https://openaccess.thecvf.com/content/CVPR2023/papers/Liao_A_Light_Weight_Model_for_Active_Speaker_Detection_CVPR_2023_paper.pdf
