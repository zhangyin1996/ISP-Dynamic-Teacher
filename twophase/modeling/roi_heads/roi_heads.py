import torch
from typing import Dict, List, Optional, Tuple, Union
from detectron2.structures import Boxes, ImageList, Instances, pairwise_iou
from detectron2.modeling.proposal_generator.proposal_utils import (
    add_ground_truth_to_proposals,
)
from detectron2.utils.events import get_event_storage
from detectron2.modeling.roi_heads.box_head import build_box_head
from detectron2.layers import ShapeSpec
from detectron2.modeling.roi_heads import (
    ROI_HEADS_REGISTRY,
    StandardROIHeads,
)
from twophase.modeling.roi_heads.fast_rcnn import FastRCNNOutputLayers
from twophase.modeling.roi_heads.fast_rcnn import FastRCNNFocaltLossOutputLayers

import numpy as np
from detectron2.modeling.poolers import ROIPooler
from torch import nn

@ROI_HEADS_REGISTRY.register()
class StandardROIHeadsPseudoLab(StandardROIHeads):
    @classmethod
    def _init_box_head(cls, cfg, input_shape):
        # fmt: off
        in_features       = cfg.MODEL.ROI_HEADS.IN_FEATURES
        pooler_resolution = cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION
        pooler_scales     = tuple(1.0 / input_shape[k].stride for k in in_features)
        sampling_ratio    = cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO
        pooler_type       = cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
        # fmt: on

        in_channels = [input_shape[f].channels for f in in_features]
        # Check all channel counts are equal
        assert len(set(in_channels)) == 1, in_channels
        in_channels = in_channels[0]

        box_pooler = ROIPooler(
            output_size=pooler_resolution,
            scales=pooler_scales,
            sampling_ratio=sampling_ratio,
            pooler_type=pooler_type,
        )
        box_head = build_box_head(
            cfg,
            ShapeSpec(
                channels=in_channels, height=pooler_resolution, width=pooler_resolution
            ),
        )

        proxy_head = None if cfg.MODEL.STUDENT_DUAL_DA is False else build_box_head(
            cfg,
            ShapeSpec(
                channels=in_channels, height=pooler_resolution, width=pooler_resolution
            ),
        )

        if cfg.MODEL.ROI_HEADS.LOSS == "CrossEntropy":
            box_predictor = FastRCNNOutputLayers(cfg, box_head.output_shape)
            if proxy_head is not None:
                proxy_predictor = FastRCNNOutputLayers(cfg, proxy_head.output_shape)
        elif cfg.MODEL.ROI_HEADS.LOSS == "FocalLoss":
            box_predictor = FastRCNNFocaltLossOutputLayers(cfg, box_head.output_shape)
            if proxy_head is not None:
                proxy_predictor = FastRCNNFocaltLossOutputLayers(cfg, proxy_head.output_shape)
        else:
            raise ValueError("Unknown ROI head loss.")

        box_heads = nn.ModuleList([box_head, proxy_head]) if proxy_head is not None else box_head
        box_predictors = nn.ModuleList([box_predictor, proxy_predictor]) if proxy_head is not None else box_predictor

        return {
            "box_in_features": in_features,
            "box_pooler": box_pooler,
            "box_head": box_heads,
            "box_predictor": box_predictors,
        }

    def forward(
        self,
        images: ImageList,
        features: Dict[str, torch.Tensor],
        proposals: List[Instances],
        targets: Optional[List[Instances]] = None,
        compute_loss=True,
        branch="",
        compute_val_loss=False,
        proposal_index=None,
    ) -> Tuple[List[Instances], Dict[str, torch.Tensor]]:

        #Check where unsup_data_consistency and target_consistency will fall
        #Return proposal_index for target_consistency and add proposal_index for unsup_data_consistency

        del images
        if (self.training and compute_loss) or branch.split('_')[0] == 'unsupdata':
            assert targets
            # 1000 --> 512

            #sup target goes here
            proposals = self.label_and_sample_proposals(
                proposals, targets, branch=branch
            )
        elif compute_val_loss:  # apply if val loss
            assert targets
            # 1000 --> 512
            temp_proposal_append_gt = self.proposal_append_gt
            self.proposal_append_gt = False
            proposals = self.label_and_sample_proposals(
                proposals, targets, branch=branch
            )  # do not apply target on proposals
            self.proposal_append_gt = temp_proposal_append_gt
        del targets

        if (self.training and compute_loss) or compute_val_loss:

            losses, predictions = self._forward_box(
                features, proposals, compute_loss, compute_val_loss, branch
            )
            return (proposals, predictions) , losses
        else:
            if branch.split('_')[0] == 'unsupdata':
                pred_instances, pred_index = self._forward_box(
                    features, proposals, compute_loss, compute_val_loss, branch
                )
                return pred_instances, proposals, pred_index

            pred_instances, predictions = self._forward_box(
                features, proposals, compute_loss, compute_val_loss, branch
            )
            return pred_instances, predictions

    def _forward_box(
        self,
        features: Dict[str, torch.Tensor],
        proposals: List[Instances],
        compute_loss: bool = True,
        compute_val_loss: bool = False,
        branch: str = "",
        proposal_index: List[torch.Tensor] = None,
    ) -> Union[Dict[str, torch.Tensor], List[Instances]]:

        #Check where unsup_data_consistency and target_consistency will fall
        #Return proposal_index for target_consistency and add proposal_index for unsup_data_consistency

        features = [features[f] for f in self.box_in_features]
        box_features = self.box_pooler(features, [x.proposal_boxes for x in proposals])

        if isinstance(self.box_head, nn.ModuleList):
            box_features = self.box_head[0](box_features)
            predictions = self.box_predictor[0](box_features)
        else:
            box_features = self.box_head(box_features)
            predictions = self.box_predictor(box_features)
        del box_features

        if (
            self.training and compute_loss
        ) or compute_val_loss:  # apply if training loss or val loss
            if isinstance(self.box_head, nn.ModuleList):
                losses = self.box_predictor[0].losses(predictions, proposals)
                for p in self.parameters():
                    losses['loss_cls'] += 0.0 * p.sum()
            else:
                losses = self.box_predictor.losses(predictions, proposals)
            if self.train_on_pred_boxes:
                with torch.no_grad():
                    pred_boxes = self.box_predictor.predict_boxes_for_gt_classes(
                        predictions, proposals
                    )
                    for proposals_per_image, pred_boxes_per_image in zip(
                        proposals, pred_boxes
                    ):
                        proposals_per_image.proposal_boxes = Boxes(pred_boxes_per_image)
            return losses, predictions
        else:
            if branch.split('_')[0] == 'unsupdata':
                if branch.split('_')[1] == 'stu':
                    pred_instances, pred_index = self.box_predictor[0].inference(predictions, proposals)
                else:
                    pred_instances, pred_index = self.box_predictor[1].inference(predictions, proposals)
                return pred_instances, pred_index
            else:
                if isinstance(self.box_head, nn.ModuleList):
                    pred_instances, pred_index = self.box_predictor[0].inference(predictions, proposals)
                else:
                    pred_instances, pred_index = self.box_predictor.inference(predictions, proposals)
                return pred_instances, predictions

    @torch.no_grad()
    def label_and_sample_proposals(
        self, proposals: List[Instances], targets: List[Instances], branch: str = ""
    ) -> List[Instances]:
        gt_boxes = [x.gt_boxes for x in targets]
        if self.proposal_append_gt:
            proposals = add_ground_truth_to_proposals(gt_boxes, proposals)

        proposals_with_gt = []

        num_fg_samples = []
        num_bg_samples = []
        for proposals_per_image, targets_per_image in zip(proposals, targets):
            has_gt = len(targets_per_image) > 0
            match_quality_matrix = pairwise_iou(
                targets_per_image.gt_boxes, proposals_per_image.proposal_boxes
            )
            matched_idxs, matched_labels = self.proposal_matcher(match_quality_matrix)
            sampled_idxs, gt_classes = self._sample_proposals(
                matched_idxs, matched_labels, targets_per_image.gt_classes
            )

            proposals_per_image = proposals_per_image[sampled_idxs]
            proposals_per_image.gt_classes = gt_classes

            if has_gt:
                sampled_targets = matched_idxs[sampled_idxs]
                for (trg_name, trg_value) in targets_per_image.get_fields().items():
                    if trg_name.startswith("gt_") and not proposals_per_image.has(
                        trg_name
                    ):
                        proposals_per_image.set(trg_name, trg_value[sampled_targets])
            else:
                gt_boxes = Boxes(
                    targets_per_image.gt_boxes.tensor.new_zeros((len(sampled_idxs), 4))
                )
                proposals_per_image.gt_boxes = gt_boxes

            num_bg_samples.append((gt_classes == self.num_classes).sum().item())
            num_fg_samples.append(gt_classes.numel() - num_bg_samples[-1])
            proposals_with_gt.append(proposals_per_image)

        storage = get_event_storage()
        storage.put_scalar(
            "roi_head/num_target_fg_samples_" + branch, np.mean(num_fg_samples)
        )
        storage.put_scalar(
            "roi_head/num_target_bg_samples_" + branch, np.mean(num_bg_samples)
        )

        return proposals_with_gt
