# ISP Dynamic Teacher
This is the code for our extended version of AAAI2024 paper ISP-Teacher.
+ The **Environment**, **Dataset**, **Train command** and **Evaluation command** are the same as [[ISP-Teacher](https://github.com/zhangyin1996/ISP-Teacher)]

## 1. Environment 
+ Detectron2==0.6  [[Install Link](https://detectron2.readthedocs.io/en/latest/tutorials/install.html)]  **Important !!!**
+ Install the appropriate versions of PyTorch and torchvision for your machine.
+ **In our setting:** 
`Cuda==10.2, Python==3.8, Pytorch==1.10.1, Detectron2==0.6`

## 2. Datasets
+ Download the BDD100k or SHIFT datasets.
+ Split dataset into two parts using labels ‘day’ and ‘night’. Convert datasets labels to coco format. You can download split json file from [Baiduyun](https://pan.baidu.com/s/1lExTex7JjZ9-4DZ_fWciSg?pwd=1234)(password:1234) or [Google Drive](https://drive.google.com/drive/folders/1ynapIcAm5subozk0QNzrPWHwWj_xpGle?usp=drive_link). Please refer to [2PCNet](https://github.com/mecarill/2pcnet) (CVPR2023) for more details.
+ Replace the dataset and label path in `twophase/data/datasets/builtin.py #188~#212` with you own.


## 3. Train
+ For BDD100k as an example, the command for training ISP-Dynamic-Teacher on 4 RTX6000 GPUs is as following:
```
python train_net.py --num-gpus 4 --config configs/faster_rcnn_R50_bdd100k.yaml OUTPUT_DIR output/bdd100k
```
## 4. Results

![image text](https://github.com/zhangyin1996/ISP-Dynamic-Teacher/blob/main/results.jpg)
