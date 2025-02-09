from detectron2.config import CfgNode as CN


def add_teacher_config(cfg):
    """
    Add config for semisupnet.
    """
    _C = cfg
    _C.TEST.VAL_LOSS = True

    _C.MODEL.RPN.UNSUP_LOSS_WEIGHT = 1.0
    _C.MODEL.RPN.LOSS = "CrossEntropy"
    _C.MODEL.ROI_HEADS.LOSS = "CrossEntropy"
    _C.MODEL.STUDENT_DUAL_DA = True

    _C.SOLVER.IMG_PER_BATCH_LABEL = 1
    _C.SOLVER.IMG_PER_BATCH_UNLABEL = 1
    _C.SOLVER.FACTOR_LIST = (1,)
    _C.SOLVER.BASE_LR = 0.0225
    _C.DATASETS.TRAIN_LABEL = ("coco_2017_train",)
    _C.DATASETS.TRAIN_UNLABEL = ("coco_2017_train",)
    _C.DATASETS.CROSS_DATASET = True
    _C.TEST.EVALUATOR = "COCOeval"

    _C.SEMISUPNET = CN()

    # Output dimension of the MLP projector after `res5` block
    _C.SEMISUPNET.MLP_DIM = 128

    # Semi-supervised training
    _C.SEMISUPNET.Trainer = "studentteacher"
    _C.SEMISUPNET.BBOX_THRESHOLD = 0.7
    _C.SEMISUPNET.IOU_THRESHOLD = 0.7
    _C.SEMISUPNET.PSEUDO_BBOX_SAMPLE = "thresholding"
    _C.SEMISUPNET.TEACHER_UPDATE_ITER = 1
    _C.SEMISUPNET.BURN_UP_STEP = 12000
    _C.SEMISUPNET.EMA_KEEP_RATE = 0.0
    _C.SEMISUPNET.UNSUP_LOSS_WEIGHT = 4.0
    _C.SEMISUPNET.SUP_LOSS_WEIGHT = 0.5
    _C.SEMISUPNET.LOSS_WEIGHT_TYPE = "standard"
    _C.SEMISUPNET.DIS_TYPE = "res4"
    _C.SEMISUPNET.DIS_LOSS_WEIGHT = 0.1
    _C.SEMISUPNET.SCALE_STEPS=(0,)
    _C.SEMISUPNET.SCALE_LIST=(1.0,)
    _C.SEMISUPNET.COOPERATED_BBOX=True
    _C.SEMISUPNET.PROXY_EMA_KEEP_RATE=0.0
    _C.SEMISUPNET.PROXY_STEP = 56000
    _C.SEMISUPNET.THR_ITER = 64000
    _C.SEMISUPNET.THR_UPDATE_ITER = 1
    _C.SEMISUPNET.THR_UPDATE_GAMMA = 0.05
    _C.SEMISUPNET.START_UNSUP_LOSS = 12000

    # dataloader
    # supervision level
    _C.DATALOADER.SUP_PERCENT = 100.0  # 5 = 5% dataset as labeled set
    _C.DATALOADER.RANDOM_DATA_SEED = 0  # random seed to read data
    _C.DATALOADER.RANDOM_DATA_SEED_PATH = "dataseed/COCO_supervision.txt"

    _C.EMAMODEL = CN()
    _C.EMAMODEL.SUP_CONSIST = True

    _C.NIGHTAUG = True
    _C.STUDENT_SCALE = True
    _C.CONSISTENCY = True

    _C.BLOCKED_CLASSES = []

    _C.CLS_LOSS = True
    _C.LOG_LOSS = True
    _C.LOSS_REG_VAL = 1.0

    _C.MIXUP = True
    _C.LIMIT_MIX = True
    _C.MIX_RATIO = 0.5
    _C.KEEP_ASPECT = False
    _C.MIX_RANDOM_CLASSES = False
    _C.DIA_LOSS = False