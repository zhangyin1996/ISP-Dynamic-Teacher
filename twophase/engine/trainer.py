import os
import time
import logging
import json
import torch
from torch.nn.parallel import DistributedDataParallel
from fvcore.nn.precise_bn import get_bn_modules
import numpy as np
from collections import OrderedDict
import torchvision.transforms as T

import detectron2.utils.comm as comm
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.engine import DefaultTrainer, SimpleTrainer, TrainerBase
from detectron2.engine.train_loop import AMPTrainer
from detectron2.utils.events import EventStorage
from detectron2.evaluation import verify_results, DatasetEvaluators
# from detectron2.evaluation import COCOEvaluator, verify_results, DatasetEvaluators

from detectron2.data.dataset_mapper import DatasetMapper
from detectron2.engine import hooks
from detectron2.structures.boxes import Boxes
from detectron2.structures.instances import Instances
from detectron2.utils.env import TORCH_VERSION
from detectron2.data import MetadataCatalog
from detectron2.structures import pairwise_iou

from twophase.data.build import (
    build_detection_semisup_train_loader,
    build_detection_test_loader,
    build_detection_semisup_train_loader_two_crops,
)
from twophase.data.dataset_mapper import DatasetMapperTwoCropSeparate
from twophase.engine.hooks import LossEvalHook
from twophase.modeling.meta_arch.ts_ensemble import EnsembleTSModel
from twophase.checkpoint.detection_checkpoint import DetectionTSCheckpointer
from twophase.solver.build import build_lr_scheduler
from twophase.evaluation import PascalVOCDetectionEvaluator, COCOEvaluator
from twophase.modeling.custom_losses import ConsistencyLosses
from twophase.data.transforms.night_aug import NightAug
import copy

from twophase.icrm import ICRm

# Adaptive Teacher Trainer
class TwoPCTrainer(DefaultTrainer):
    def __init__(self, cfg):
        """
        Args:
            cfg (CfgNode):
        Use the custom checkpointer, which loads other backbone models
        with matching heuristics.
        """
        cfg = DefaultTrainer.auto_scale_workers(cfg, comm.get_world_size())
        data_loader = self.build_train_loader(cfg)

        # create an 【Student】 model
        model = self.build_model(cfg)
        optimizer = self.build_optimizer(cfg, model)

        # create an 【Teacher】 model
        model_teacher = self.build_model(cfg)
        self.model_teacher = model_teacher

        # For training, wrap with DDP. But don't need this for inference.
        if comm.get_world_size() > 1:
            model = DistributedDataParallel(
                model, device_ids=[comm.get_local_rank()], broadcast_buffers=False
            )

        TrainerBase.__init__(self)
        self._trainer = (AMPTrainer if cfg.SOLVER.AMP.ENABLED else SimpleTrainer)(
            model, data_loader, optimizer
        )
        self.scheduler = self.build_lr_scheduler(cfg, optimizer)


        ensem_ts_model = EnsembleTSModel(model_teacher, model)

        self.checkpointer = DetectionTSCheckpointer(
            ensem_ts_model,
            cfg.OUTPUT_DIR,
            optimizer=optimizer,
            scheduler=self.scheduler,
        )
        self.start_iter = 0
        self.stu_scale = None
        self.max_iter = cfg.SOLVER.MAX_ITER
        self.scale_list = np.array(cfg.SEMISUPNET.SCALE_LIST)
        self.scale_checkpoints = np.array(cfg.SEMISUPNET.SCALE_STEPS)
        self.cfg = cfg
        self.ext_data = []
        self.img_vals = {}
        self.consistency_losses = ConsistencyLosses()
        self.night_aug = NightAug()

        self.register_hooks(self.build_hooks())

        ###self.probe = OpenMatchTrainerProbe(cfg)
        self.icrm = ICRm(cfg.MODEL.ROI_HEADS.NUM_CLASSES, 50, cfg.OUTPUT_DIR, blocked_classes=self.cfg.BLOCKED_CLASSES,
                         mix_ratio=self.cfg.MIX_RATIO, cfg=self.cfg)
        self.top_eval_ap = 0.0
        self.top_eval_ap_stu = 0.0

    def resume_or_load(self, resume=True):
        """
        If `resume==True` and `cfg.OUTPUT_DIR` contains the last checkpoint (defined by
        a `last_checkpoint` file), resume from the file. Resuming means loading all
        available states (eg. optimizer and scheduler) and update iteration counter
        from the checkpoint. ``cfg.MODEL.WEIGHTS`` will not be used.
        Otherwise, this is considered as an independent training. The method will load model
        weights from the file `cfg.MODEL.WEIGHTS` (but will not load other states) and start
        from iteration 0.
        Args:
            resume (bool): whether to do resume or not
        """
        checkpoint = self.checkpointer.resume_or_load(
            self.cfg.MODEL.WEIGHTS, resume=resume
        )
        if resume: # and self.checkpointer.has_checkpoint():
            self.start_iter = checkpoint.get("iteration", -1) + 1
            # The checkpoint stores the training iteration that just finished, thus we start
            # at the next iteration (or iter zero if there's no checkpoint).
        if isinstance(self.model, DistributedDataParallel):
            # broadcast loaded data/model from the first rank, because other
            # machines may not have access to the checkpoint file
            if TORCH_VERSION >= (1, 7):
                self.model._sync_params_and_buffers()
            self.start_iter = comm.all_gather(self.start_iter)[0]

    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
        evaluator_list = []
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type

        if evaluator_type == "coco":
            evaluator_list.append(COCOEvaluator(
                dataset_name, output_dir=output_folder))
        elif evaluator_type == "pascal_voc":
            return PascalVOCDetectionEvaluator(dataset_name)
        elif evaluator_type == "pascal_voc_water":
            return PascalVOCDetectionEvaluator(dataset_name, target_classnames=["bicycle", "bird", "car", "cat", "dog", "person"])
        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        elif len(evaluator_list) == 1:
            return evaluator_list[0]

        return DatasetEvaluators(evaluator_list)

    @classmethod
    def build_train_loader(cls, cfg):
        mapper = DatasetMapperTwoCropSeparate(cfg, True)
        return build_detection_semisup_train_loader_two_crops(cfg, mapper)

    @classmethod
    def build_lr_scheduler(cls, cfg, optimizer):
        return build_lr_scheduler(cfg, optimizer)

    def train(self):
        self.train_loop(self.start_iter, self.max_iter)
        if hasattr(self, "_last_eval_results") and comm.is_main_process():
            verify_results(self.cfg, self._last_eval_results)
            return self._last_eval_results

    def train_loop(self, start_iter: int, max_iter: int):
        logger = logging.getLogger(__name__)
        logger.info("Starting training from iteration {}".format(start_iter))

        self.iter = self.start_iter = start_iter
        self.max_iter = max_iter

        with EventStorage(start_iter) as self.storage:
            try:
                self.before_train()

                for self.iter in range(start_iter, max_iter):
                    self.before_step()
                    self.run_step_full_semisup()
                    self.after_step()
            except Exception:
                logger.exception("Exception during training:")
                raise
            finally:
                self.after_train()

    # =====================================================
    # ================== Pseduo-labeling ==================
    # =====================================================
    def threshold_bbox(self, proposal_bbox_inst, thres=0.7, proposal_type="roih"):
        if proposal_type == "rpn":
            valid_map = proposal_bbox_inst.objectness_logits > thres

            # create instances containing boxes and gt_classes
            image_shape = proposal_bbox_inst.image_size
            new_proposal_inst = Instances(image_shape)

            # create box
            new_bbox_loc = proposal_bbox_inst.proposal_boxes.tensor[valid_map, :]
            new_boxes = Boxes(new_bbox_loc)

            # add boxes to instances
            new_proposal_inst.gt_boxes = new_boxes
            new_proposal_inst.objectness_logits = proposal_bbox_inst.objectness_logits[
                valid_map
            ]
        elif proposal_type == "roih":
            valid_map = proposal_bbox_inst.scores > thres

            full_scores = proposal_bbox_inst.full_scores.clone()

            # create instances containing boxes and gt_classes
            image_shape = proposal_bbox_inst.image_size
            new_proposal_inst = Instances(image_shape)

            # create box
            new_bbox_loc = proposal_bbox_inst.pred_boxes.tensor[valid_map, :]
            new_boxes = Boxes(new_bbox_loc)

            # add boxes to instances
            new_proposal_inst.gt_boxes = new_boxes
            new_proposal_inst.gt_classes = proposal_bbox_inst.pred_classes[valid_map]
            new_proposal_inst.scores = proposal_bbox_inst.scores[valid_map]
            new_proposal_inst.full_scores = full_scores[valid_map]

        return new_proposal_inst


    # def is_max_value(boxes, index, dim=0):
    #     return (boxes[index] == boxes.max(dim=dim).values).all()

    def cooperated_bbox_learning(self, proposal_bbox_inst, cls_thres=0.8, iou_thres=0.8, proposal_type="roih",
                                 student_proposals=None):
        if proposal_type == "roih":
            valid_map = proposal_bbox_inst.scores > cls_thres

            full_scores = proposal_bbox_inst.full_scores.clone()

            filter_cls_confidence = []
            match_quality_matrix = pairwise_iou(
                proposal_bbox_inst.pred_boxes, student_proposals.pred_boxes
            )
            match_quality_matrix_diag_filter = match_quality_matrix > iou_thres
            for i in range(match_quality_matrix_diag_filter.shape[0]):
                for j in range(match_quality_matrix_diag_filter.shape[1]):

                    if match_quality_matrix_diag_filter[i][j] == True and valid_map[i] == False:
                        if student_proposals.pred_classes[j] == proposal_bbox_inst.pred_classes[i] and proposal_bbox_inst.scores[i] >= 0.5:

                            valid_map[i] = True

                            if proposal_bbox_inst.scores[i] < cls_thres:
                                filter_cls_confidence.append(proposal_bbox_inst.scores[i].detach().cpu().float().item())

            image_shape = proposal_bbox_inst.image_size
            new_proposal_inst = Instances(image_shape)

            new_bbox_loc = proposal_bbox_inst.pred_boxes.tensor[valid_map, :]
            new_boxes = Boxes(new_bbox_loc)

            new_proposal_inst.gt_boxes = new_boxes
            new_proposal_inst.gt_classes = proposal_bbox_inst.pred_classes[valid_map]
            new_proposal_inst.scores = proposal_bbox_inst.scores[valid_map]

            new_proposal_inst.full_scores = full_scores[valid_map]

            return new_proposal_inst, filter_cls_confidence

    def process_pseudo_label(
            self, proposals_rpn_unsup_k, cur_threshold, iou_threshold, proposal_type, psedo_label_method="",
            student_proporsals=None):
        list_instances = []
        num_proposal_output = 0.0
        stu_tea_matched_results = []
        matched_results = {}
        for idx, proposal_bbox_inst in enumerate(proposals_rpn_unsup_k):
            # thresholding
            if psedo_label_method == "thresholding":
                proposal_bbox_inst = self.threshold_bbox(
                    proposal_bbox_inst, thres=cur_threshold, proposal_type=proposal_type
                )
            elif psedo_label_method == "cooperated":
                proposal_bbox_inst, matched_results = self.cooperated_bbox_learning(
                    proposal_bbox_inst, cls_thres=cur_threshold, iou_thres=iou_threshold, proposal_type=proposal_type,
                    student_proposals=student_proporsals[idx],
                )
            else:
                raise ValueError("Unkown pseudo label boxes methods")
            num_proposal_output += len(proposal_bbox_inst)
            list_instances.append(proposal_bbox_inst)
            stu_tea_matched_results.extend(matched_results)
        num_proposal_output = num_proposal_output / len(proposals_rpn_unsup_k)
        return list_instances, num_proposal_output, stu_tea_matched_results

    def remove_label(self, label_data):
        for label_datum in label_data:
            if "instances" in label_datum.keys():
                del label_datum["instances"]
        return label_data

    def add_label(self, unlabled_data, label):
        for unlabel_datum, lab_inst in zip(unlabled_data, label):
            unlabel_datum["instances"] = lab_inst
        return unlabled_data
    
    def get_label(self, label_data):
        label_list = []
        for label_datum in label_data:
            if "instances" in label_datum.keys():
                label_list.append(copy.deepcopy(label_datum["instances"]))
        
        return label_list

    def update_threshold(self, pre_thr, confidence_scores, gamma=0.05):

        confidence_scores = sorted(confidence_scores, reverse=True)
        top_50_percent_scores = confidence_scores[:len(confidence_scores) // 2]
        mu = sum(top_50_percent_scores) / len(top_50_percent_scores)
        sigma = (sum((x - mu) ** 2 for x in top_50_percent_scores) / len(top_50_percent_scores)) ** 0.5
        tau_stp_cls = mu - 2 * sigma
        tau_dev_cls = gamma * (tau_stp_cls - pre_thr)

        new_threshold = pre_thr + tau_dev_cls

        return new_threshold

    # =====================================================
    # =================== Training Flow ===================
    # =====================================================

    def run_step_full_semisup(self):
        self._trainer.iter = self.iter
        assert self.model.training, "[UBTeacherTrainer] model was changed to eval mode!"
        start = time.perf_counter()
        data = next(self._trainer._data_loader_iter)
        # data_q and data_k from different augmentations (q:strong, k:weak)
        # label_strong, label_weak, unlabed_strong, unlabled_weak
        label_data, unlabel_data = data
        data_time = time.perf_counter() - start

        if self.cfg.MIXUP:
            with torch.no_grad():
                self.icrm.save_crops(label_data)
                label_data = self.icrm.mix_crop_new(label_data)
        else:
            with torch.no_grad():
                self.icrm.save_crops(label_data)
                label_data = self.icrm.add_labels(label_data)

        # burn-in stage (supervised training with labeled data)
        if self.iter < self.cfg.SEMISUPNET.BURN_UP_STEP:

            if self.cfg.CLS_LOSS:
                class_info = self.icrm.class_info
            else:
                class_info = None

            record_dict, _, _, proposals_predictions = self.model(label_data, branch="supervised")
            with torch.no_grad():
                self.icrm.get_matches(proposals_predictions, self.iter)

            # weight losses
            loss_dict = {}
            for key in record_dict.keys():
                if key[:4] == "loss":
                    if key == "loss_aet":
                        loss_dict[key] = record_dict[key] * 2
                    else:
                        loss_dict[key] = record_dict[key] * 1
            losses = sum(loss_dict.values())

        # Student-teacher stage
        else:
            if self.iter == self.cfg.SEMISUPNET.BURN_UP_STEP:
                self._update_teacher_model(keep_rate=0.00)

            if self.iter == self.cfg.SEMISUPNET.PROXY_STEP:
                self._update_proxy_student_model(keep_rate=0.00)
            elif (self.iter - self.cfg.SEMISUPNET.BURN_UP_STEP) % self.cfg.SEMISUPNET.TEACHER_UPDATE_ITER == 0:
                self._update_teacher_model(keep_rate=self.cfg.SEMISUPNET.EMA_KEEP_RATE)

            record_dict = {}

            # 1. Input labeled data into student model
            # record_all_label_data, _, _, _ = self.model(label_data, branch="supervised")
            # record_dict.update(record_all_label_data)

            record_all_label_data, _, _, proposals_predictions = self.model(label_data, branch="supervised")
            record_dict.update(record_all_label_data)
            with torch.no_grad():
                self.icrm.get_matches(proposals_predictions,self.iter)

            if self.cfg.CLS_LOSS:
                if self.iter < self.cfg.SEMISUPNET.BURN_UP_STEP+400:
                    class_info = self.icrm.class_info
                else:
                    class_info = self.icrm.target_class_info
            else:
                class_info = None

            #  2. Remove unlabeled data labels
            gt_unlabel = self.get_label(unlabel_data)
            unlabel_data = self.remove_label(unlabel_data)

            #  3. Generate the [easy pseudo-label] using teacher model (Phase-1)
            with torch.no_grad():
                (_, proposals_rpn_unsup, proposals_roih_unsup, _,) = self.model_teacher(unlabel_data, branch="unsup_data_weak")

            #  4. Pseudo-labeling
            cur_threshold = self.cfg.SEMISUPNET.BBOX_THRESHOLD
            iou_threshold = self.cfg.SEMISUPNET.IOU_THRESHOLD

            joint_proposal_dict = {}

            pesudo_proposals_roih_unsup, _, _ = self.process_pseudo_label(
                proposals_roih_unsup, cur_threshold, iou_threshold, "roih", "thresholding"
            )
            joint_proposal_dict["proposals_pseudo_roih_threshold"] = pesudo_proposals_roih_unsup

            unlabel_data = self.add_label(unlabel_data, joint_proposal_dict["proposals_pseudo_roih_threshold"])

            (pseudo_losses,
             proposals_into_roih,
             rpn_proxy,
             roi_proxy,
             pred_idx) = self.model(
                unlabel_data, branch="unsupdata_proxy"
            )

            pesudo_proposals_roih_unsup, _, filter_cls_confidence = self.process_pseudo_label(
                proposals_roih_unsup, cur_threshold, iou_threshold, "roih", "cooperated", roi_proxy
            )

            if self.iter > self.cfg.SEMISUPNET.THR_ITER and self.iter % self.cfg.SEMISUPNET.THR_UPDATE_ITER == 0 and len(
                    filter_cls_confidence) > 0:
                filter_cls_confidence_temp = filter_cls_confidence
                confidence_scores = sorted(filter_cls_confidence_temp, reverse=True)
                top_50_percent_scores = confidence_scores[:len(confidence_scores) // 2]
                if len(top_50_percent_scores) != 0:
                    self.cfg.SEMISUPNET.BBOX_THRESHOLD = self.update_threshold(cur_threshold, filter_cls_confidence,
                                                                           self.cfg.SEMISUPNET.THR_UPDATE_GAMMA)

            joint_proposal_dict["proposals_pseudo_roih"] = pesudo_proposals_roih_unsup

            # 5. Add pseudo-label to unlabeled data
            unlabel_data = self.add_label(
                unlabel_data, joint_proposal_dict["proposals_pseudo_roih"]
            )

            if self.cfg.MIXUP:
                with torch.no_grad():
                    self.icrm.save_crops_target(unlabel_data)
                    unlabel_data = self.icrm.mix_crop_new(unlabel_data, True)
            else:
                with torch.no_grad():
                    self.icrm.save_crops_target(unlabel_data)
                    unlabel_data = self.icrm.add_labels(unlabel_data)

            if self.cfg.CLS_LOSS:
                class_info = self.icrm.class_info
            else:
                class_info = None

            record_all_unlabel_data, _, _, proposals_predictions_unlabel = self.model(unlabel_data, branch="supervised_target")

            with torch.no_grad():
                self.icrm.get_matches(proposals_predictions_unlabel, self.iter, True)

            #7. Input scaled inputs into student
            (pseudo_losses,
            proposals_into_roih,
            rpn_stu,
            roi_stu,
            pred_idx
             )= self.model(
                unlabel_data, branch="unsupdata_stu"
            )


            new_pseudo_losses = {key + "_pseudo": value for key, value in pseudo_losses.items()}

            record_dict.update(new_pseudo_losses)

            # weight losses
            loss_dict = {}
            for key in record_dict.keys():
                if key.startswith("loss"):
                    if key == "loss_rpn_loc_pseudo": 
                        loss_dict[key] = record_dict[key] * 0
                    elif key.endswith('loss_cls_pseudo'):
                        loss_dict[key] = record_dict[key] * self.cfg.SEMISUPNET.UNSUP_LOSS_WEIGHT
                    elif key.endswith('loss_rpn_cls_pseudo'):
                        loss_dict[key] = record_dict[key]
                    else: 
                        loss_dict[key] = record_dict[key] * 1

            losses = sum(loss_dict.values())

        metrics_dict = record_dict
        metrics_dict["data_time"] = data_time

        self._write_metrics(metrics_dict)

        self.optimizer.zero_grad()
        losses.backward()
        self.optimizer.step()

    def _write_metrics(self, metrics_dict: dict):
        metrics_dict = {
            k: v.detach().cpu().item() if isinstance(v, torch.Tensor) else float(v)
            for k, v in metrics_dict.items()
        }

        # gather metrics among all workers for logging
        # This assumes we do DDP-style training, which is currently the only
        # supported method in detectron2.
        all_metrics_dict = comm.gather(metrics_dict)
        # all_hg_dict = comm.gather(hg_dict)

        if comm.is_main_process():
            if "data_time" in all_metrics_dict[0]:
                # data_time among workers can have high variance. The actual latency
                # caused by data_time is the maximum among workers.
                data_time = np.max([x.pop("data_time")
                                   for x in all_metrics_dict])
                self.storage.put_scalar("data_time", data_time)

            # average the rest metrics
            metrics_dict = {
                k: np.mean([x[k] for x in all_metrics_dict])
                for k in all_metrics_dict[0].keys()
            }

            # append the list
            loss_dict = {}
            for key in metrics_dict.keys():
                if key[:4] == "loss":
                    loss_dict[key] = metrics_dict[key]

            total_losses_reduced = sum(loss for loss in loss_dict.values())

            self.storage.put_scalar("total_loss", total_losses_reduced)
            if len(metrics_dict) > 1:
                self.storage.put_scalars(**metrics_dict)

    @torch.no_grad()
    def _update_teacher_model(self, keep_rate=0.9996):
        if comm.get_world_size() > 1:
            student_model_dict = {
                key[7:]: value for key, value in self.model.state_dict().items()
            }
        else:
            student_model_dict = self.model.state_dict()

        new_teacher_dict = OrderedDict()
        for key, value in self.model_teacher.state_dict().items():
            if key in student_model_dict.keys():
                new_teacher_dict[key] = (
                    student_model_dict[key] *
                    (1 - keep_rate) + value * keep_rate
                )
            else:
                raise Exception("{} is not found in student model".format(key))

        self.model_teacher.load_state_dict(new_teacher_dict)

    @torch.no_grad()
    def _update_proxy_student_model(self, keep_rate=0.9996):
        if comm.get_world_size() > 1:
            student_model_dict = {
                key[7:]: value for key, value in self.model.state_dict().items()
            }
            key_pre = 'module.'
        else:
            student_model_dict = self.model.state_dict()
            key_pre=''
        new_student_dict = OrderedDict()
        for key in student_model_dict.keys():
            key_repl = key_pre + key
            if 'box' in key:
                flag = key.split('.')[2]
                if flag == '1':
                    state_key = key.replace('.1.', '.0.')
                    new_student_dict[key_repl] = (
                            student_model_dict[state_key] *
                            (1 - keep_rate) + student_model_dict[key] * keep_rate
                    )
                else:
                    new_student_dict[key_repl] = student_model_dict[key]
            else:
                new_student_dict[key_repl] = student_model_dict[key]
        self.model.load_state_dict(new_student_dict)

    @torch.no_grad()
    def _copy_main_model(self):
        # initialize all parameters
        if comm.get_world_size() > 1:
            rename_model_dict = {
                key[7:]: value for key, value in self.model.state_dict().items()
            }
            self.model_teacher.load_state_dict(rename_model_dict)
        else:
            self.model_teacher.load_state_dict(self.model.state_dict())

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        return build_detection_test_loader(cfg, dataset_name)

    def build_hooks(self):
        cfg = self.cfg.clone()
        cfg.defrost()
        cfg.DATALOADER.NUM_WORKERS = 0  # save some memory and time for PreciseBN

        ret = [
            hooks.IterationTimer(),
            hooks.LRScheduler(self.optimizer, self.scheduler),
            hooks.PreciseBN(
                # Run at the same freq as (but before) evaluation.
                cfg.TEST.EVAL_PERIOD,
                self.model,
                # Build a new data loader to not affect training
                self.build_train_loader(cfg),
                cfg.TEST.PRECISE_BN.NUM_ITER,
            )
            if cfg.TEST.PRECISE_BN.ENABLED and get_bn_modules(self.model)
            else None,
        ]

        # Do PreciseBN before checkpointer, because it updates the model and need to
        # be saved by checkpointer.
        # This is not always the best: if checkpointing has a different frequency,
        # some checkpoints may have more precise statistics than others.
        if comm.is_main_process():
            ret.append(
                hooks.PeriodicCheckpointer(
                    self.checkpointer, cfg.SOLVER.CHECKPOINT_PERIOD
                )
            )

        def test_and_save_results_student():
            self._last_eval_results_student = self.test(self.cfg, self.model)
            _last_eval_results_student = {
                k + "_student": self._last_eval_results_student[k]
                for k in self._last_eval_results_student.keys()
            }
            return _last_eval_results_student

        def test_and_save_results_teacher():
            self._last_eval_results_teacher = self.test(
                self.cfg, self.model_teacher)
            return self._last_eval_results_teacher

        ret.append(hooks.EvalHook(cfg.TEST.EVAL_PERIOD,
                   test_and_save_results_student))
        ret.append(hooks.EvalHook(cfg.TEST.EVAL_PERIOD,
                   test_and_save_results_teacher))

        if comm.is_main_process():
            # run writers in the end, so that evaluation metrics are written
            ret.append(hooks.PeriodicWriter(self.build_writers(), period=20))
        return ret

