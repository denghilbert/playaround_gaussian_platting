
#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import torch
from typing import NamedTuple, Optional
from random import randint
from utils.loss_utils import l1_loss, ssim, kl_divergence, l2_loss
from gaussian_renderer import render, network_gui
import sys
from scene import Scene, GaussianModel, SpecularModel
from utils.general_utils import safe_state, get_linear_noise_func, linear_to_srgb
import math
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from utils.visualization import wandb_image
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

import wandb
from lightglue import LightGlue, SuperPoint, DISK, SIFT, ALIKED
from lightglue.utils import load_image, rbd
from lightglue import viz2d

# set random seeds
import numpy as np
import random
seed_value = 42  # Replace this with your desired seed value

torch.manual_seed(seed_value)
if torch.cuda.is_available():
    torch.cuda.manual_seed(seed_value)
    torch.cuda.manual_seed_all(seed_value)  # if you are using multi-GPU.
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

np.random.seed(seed_value)
random.seed(seed_value)

def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from, use_wandb=False, random_init=False, hybrid=False, opt_cam=False, r_t_noise=[0., 0.]):
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree, dataset.asg_degree)
    if hybrid:
        specular_mlp = SpecularModel()
        specular_mlp.train_setting(opt)

    scene = Scene(dataset, gaussians, random_init=random_init, r_t_noise=r_t_noise)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)
    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
    mlp_color = 0

    viewpoint_stack = scene.getTrainCameras().copy()
    camera_id = [camera.uid for camera in viewpoint_stack]
    extrinsic_list = [camera.get_w2c for camera in viewpoint_stack ]
    angle_threshold = 30
    pairs = image_pair_candidates(extrinsic_list, angle_threshold, camera_id)


    # first view
    viewpoint_cam_0 = viewpoint_stack[0]

    render_pkg = render(viewpoint_cam_0, gaussians, pipe, background, mlp_color, iteration=7000, hybrid=False)
    image, viewspace_point_tensor, visibility_filter, radii = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"]
    gt_image = viewpoint_cam_0.original_image.cuda()
    # transform to wandb
    images_error = (image - gt_image).abs()
    images = {
        f"vis/rgb_target0": wandb_image(gt_image),
        f"vis/rgb_render0": wandb_image(image),
        f"vis/rgb_error0": wandb_image(images_error),
    }



    import time
    from contextlib import contextmanager
    @contextmanager
    def timer(if_print=True):
        start = time.perf_counter()
        yield
        end = time.perf_counter()
        if if_print:
            print("Elapsed Time:", end - start, "seconds")

    # view2
    match_features = ['superpoint', 'disk', 'aliked', 'sift']
    #for match_feature in match_features:
    match_feature = 'disk'
    with timer(if_print=True):
        #for i in range(len(viewpoint_stack)):
        #for i in [39, 61, 0]:
        #for i in [4]:
        count = -1
        for viewpoint_cam_0 in viewpoint_stack:
            count = count + 1
            print(count)
            render_pkg = render(viewpoint_cam_0, gaussians, pipe, background, mlp_color, iteration=7000, hybrid=False)
            image = render_pkg["render"]
            for i in pairs[viewpoint_cam_0.uid]:
                viewpoint_cam_1 = viewpoint_stack[i]
                render_pkg = render(viewpoint_cam_1, gaussians, pipe, background, mlp_color, iteration=7000, hybrid=False)
                image_1 = render_pkg["render"]
                gt_image = viewpoint_cam_1.original_image.cuda()

                with torch.no_grad():
                    m_kpts0, m_kpts1 = light_glue_simple(image, image_1, match_feature)
                    #matched_imgs, correspondence_img, m_kpts0, m_kpts1 = light_glue(image, image_1, match_feature)
                    #matched_imgs = matched_imgs[:, :, :3].permute(2, 0, 1)
                    #correspondence_img = correspondence_img[:, :, :3].permute(2, 0, 1)

                #wandb_matched_img = matched_imgs.unsqueeze(0).detach()
                #wandb_correspondence_img = correspondence_img.unsqueeze(0).detach()
                #images_error = (gt_image - image_1).abs()
                #images.update(
                #    {f"vis/rgb_target{i}": wandb_image(gt_image),
                #    f"vis/rgb_render{i}": wandb_image(image_1),
                #    f"vis/rgb_error{i}": wandb_image(images_error),
                #    f"{match_feature}/matched_img{i}": wandb_image(wandb_matched_img),
                #    f"{match_feature}/correspondence_img{i}": wandb_image(wandb_correspondence_img)}
                #)

                #if use_wandb:
                #    wandb.log(images, step=0)

    # img0 and img4
    # 0 xy [666, 402], [287, 156], [455, 786]
    # 1 xy [668, 224], [261, 182], [603, 670]
    # x is on width axis which is col
    #img0_row_col = [[402, 156, 786], [666, 287, 455]]
    #img1_row_col = [[224, 182, 670], [668, 261, 603]]
    #points_img0 = torch.tensor([[666, 402],
    #                            [287, 156],
    #                            [455, 786]]).cuda()
    #points_img1 = torch.tensor([[668, 224],
    #                            [261, 182],
    #                            [603, 670]]).cuda()

    points_img0 = m_kpts0
    points_img1 = m_kpts1
    img0_row_col = m_kpts0.t()[[1, 0], :]
    img1_row_col = m_kpts1.t()[[1, 0], :]
    points_proj_img0, points_proj_img1, valid = correspondence_projection(img0_row_col, img1_row_col, viewpoint_cam_0, viewpoint_cam_1, projection_type='average') # average, self, separate

    point_dists_0 = dist_point_point(points_img0[valid], points_proj_img0[valid])
    point_dists_1 = dist_point_point(points_img1[valid], points_proj_img1[valid])
    proj_ray_dist_threshold = 5.0

    loss = projection_loss(point_dists_0, point_dists_1, proj_ray_dist_threshold)

def image_pair_candidates(extrinsics, pairing_angle_threshold, i_map=None):
    """
    i_map is used when provided extrinsics are not having sequentiall
    index. i_map is a list of ints where each element corresponds to
    image index.
    """

    pairs = {}

    assert i_map is None or len(i_map) == len(extrinsics)

    num_images = len(extrinsics)

    for i in range(num_images):

        rot_mat_i = extrinsics[i][:3, :3]

        for j in range(i + 1, num_images):

            rot_mat_j = extrinsics[j][:3, :3]
            rot_mat_ij = rot_mat_i @ rot_mat_j.inverse()
            angle_rad = torch.acos((torch.trace(rot_mat_ij) - 1) / 2)
            angle_deg = angle_rad / np.pi * 180

            if torch.abs(angle_deg) < pairing_angle_threshold:

                i_entry = i if i_map is None else i_map[i]
                j_entry = j if i_map is None else i_map[j]

                if not i_entry in pairs.keys():
                    pairs[i_entry] = []
                if not j_entry in pairs.keys():
                    pairs[j_entry] = []

                pairs[i_entry].append(j_entry)
                pairs[j_entry].append(i_entry)

    return pairs

def projection_loss(point_dists_0, point_dists_1, proj_ray_dist_threshold):

    loss0_valid_idx = torch.logical_and(
        point_dists_0 < proj_ray_dist_threshold,
        torch.isfinite(point_dists_0)
    )
    loss1_valid_idx = torch.logical_and(
        point_dists_1 < proj_ray_dist_threshold,
        torch.isfinite(point_dists_1)
    )
    loss0 = point_dists_0[loss0_valid_idx].mean()
    loss1 = point_dists_1[loss1_valid_idx].mean()

    return 0.5 * (loss0 + loss1)


def dist_point_line(a, b, c):
    """
    a, b, c: [number of points, 3]
    return: distance of a to bc
    """
    cross_product = torch.cross(a - b, a - c)
    cross_norm = torch.norm(cross_product, dim=1)
    BC_norm = torch.norm(c - b, dim=1)
    return cross_norm / BC_norm

def dist_point_point(ref_points, points):
    return torch.sqrt(torch.sum((ref_points - points)**2, dim=1))

def epipolar_correspondence_test(points, intrinsic0, intrinsic1, w2c0, w2c1):
    """
    points: [1, 3, 4], [1, number of points, xyz1]
    intrinsic: [3, 3]
    w2c: [4, 4]
    return: points_to_img0, points_to_img1, origin0_to_img1, origin1_to_img0
    """
    origin0 = w2c0.inverse()[:4, -1].expand(points.shape)
    origin1 = w2c1.inverse()[:4, -1].expand(points.shape)

    # project 3d points
    p_proj_to_im0 = torch.einsum("ijk, pk -> ijp", points, w2c0[:3, :])
    p_proj_to_im1 = torch.einsum("ijk, pk -> ijp", points, w2c1[:3, :])
    p_norm_im0 = torch.einsum("ijk, pk -> ijp", p_proj_to_im0, intrinsic0)
    p_norm_im1 = torch.einsum("ijk, pk -> ijp", p_proj_to_im1, intrinsic1)
    p_norm_im0_2d = p_norm_im0[:, :, :2] / (p_norm_im0[:, :, 2, None] + 1e-10)
    p_norm_im1_2d = p_norm_im1[:, :, :2] / (p_norm_im1[:, :, 2, None] + 1e-10)

    # project origins
    ori1_proj_to_im0 = torch.einsum("ijk, pk -> ijp", origin1, w2c0[:3, :])
    ori0_proj_to_im1 = torch.einsum("ijk, pk -> ijp", origin0, w2c1[:3, :])
    ori1_norm_im0 = torch.einsum("ijk, pk -> ijp", ori1_proj_to_im0, intrinsic0)
    ori0_norm_im1 = torch.einsum("ijk, pk -> ijp", ori0_proj_to_im1, intrinsic1)
    ori1_norm_im0_2d = ori1_norm_im0[:, :, :2] / (ori1_norm_im0[:, :, 2, None] + 1e-10)

    return p_norm_im0_2d[0], p_norm_im1_2d[0], ori0_norm_im1_2d[0], ori1_norm_im0_2d[0]

def direction_origin_interpolation(img_row_col, rays_d_o):
    lb2center = torch.floor(img_row_col) - img_row_col
    rt2center = torch.ceil(img_row_col) - img_row_col
    lt2center = torch.stack((lb2center[0], rt2center[1]))
    rb2center = torch.stack((rt2center[0], lb2center[1]))

    w0 = torch.norm(lb2center, p=2, dim=0).unsqueeze(-1)
    w1 = torch.norm(rt2center, p=2, dim=0).unsqueeze(-1)
    w2 = torch.norm(lt2center, p=2, dim=0).unsqueeze(-1)
    w3 = torch.norm(rb2center, p=2, dim=0).unsqueeze(-1)

    left_bottom = torch.floor(img_row_col).int().tolist()
    right_top = torch.ceil(img_row_col).int().tolist()
    direction_lb = rays_d_o[left_bottom[0], left_bottom[1], :]
    direction_lt = rays_d_o[left_bottom[0], right_top[1], :]
    direction_rb = rays_d_o[right_top[0], left_bottom[1], :]
    direction_rt = rays_d_o[right_top[0], right_top[1], :]

    return (direction_lb * w0 + direction_rt * w1 + direction_lt * w2 + direction_rb * w3) / (w0 + w1 + w2 + w3)


def correspondence_projection(img0_row_col, img1_row_col, viewpoint_cam_0, viewpoint_cam_1, projection_type='separate'):
    """
    img_row_col: [2, number_points]
    viewpoint_cam_i: camera information
    return: points_projected_img0, points_projected_img1, valid
    """

    rays_o_0, rays_d_0 = viewpoint_cam_0.get_rays
    rays_o_1, rays_d_1 = viewpoint_cam_1.get_rays
    direction0 = direction_origin_interpolation(img0_row_col, rays_d_0)
    direction1 = direction_origin_interpolation(img1_row_col, rays_d_1)
    origin0 = rays_o_0.reshape(-1, rays_o_0.shape[-1])[:direction0.shape[0]]
    origin1 = rays_o_1.reshape(-1, rays_o_1.shape[-1])[:direction1.shape[0]]

    direction0 = direction0.unsqueeze(0)
    direction1 = direction1.unsqueeze(0)
    origin0 = origin0.unsqueeze(0)
    origin1 = origin1.unsqueeze(0)
    direction0 = direction0 / (direction0.norm(p=2, dim=-1)[:, :, None] + 1e-10)
    direction1 = direction1 / (direction1.norm(p=2, dim=-1)[:, :, None] + 1e-10)

    origin0 = torch.cat([origin0, torch.ones((origin0.shape[:2]), device='cuda')[:, :, None]], dim=-1)[:, :, :3]
    origin1 = torch.cat([origin1, torch.ones((origin1.shape[:2]), device='cuda')[:, :, None]], dim=-1)[:, :, :3]

    intrinsic0 = viewpoint_cam_0.get_intrinsic
    intrinsic1 = viewpoint_cam_1.get_intrinsic

    # we have different x axis from Multiple view geometry Book
    #intrinsic0[0][0] = -intrinsic0[0][0]
    #intrinsic1[0][0] = -intrinsic1[0][0]
    w2c0 = viewpoint_cam_0.get_w2c[:3, :]
    w2c1 = viewpoint_cam_1.get_w2c[:3, :]

    p0_4d, p1_4d, valid = lines_intersect(origin0, origin1, direction0, direction1)

    avg_p0_p1 = (p0_4d + p1_4d) / 2

    if projection_type == 'self':
        # project 3d points (world coordinate) back to original images to see if projection is correct..
        #####
        p0_proj_to_im0 = torch.einsum("ijk, pk -> ijp", p0_4d, w2c0)
        p1_proj_to_im1 = torch.einsum("ijk, pk -> ijp", p1_4d, w2c1)
        p0_norm_im0 = torch.einsum("ijk, pk -> ijp", p0_proj_to_im0, intrinsic0)
        p1_norm_im1 = torch.einsum("ijk, pk -> ijp", p1_proj_to_im1, intrinsic1)
        p0_norm_im0_2d = p0_norm_im0[:, :, :2] / (p0_norm_im0[:, :, 2, None] + 1e-10)
        p1_norm_im1_2d = p1_norm_im1[:, :, :2] / (p1_norm_im1[:, :, 2, None] + 1e-10)
        return p0_norm_im0_2d[0], p1_norm_im1_2d[0], valid
        #####


    if projection_type == 'separate':
        # project 3d points to target image
        #####
        p0_proj_to_im1 = torch.einsum("ijk, pk -> ijp", p0_4d, w2c1)
        p1_proj_to_im0 = torch.einsum("ijk, pk -> ijp", p1_4d, w2c0)

        p0_norm_im1 = torch.einsum("ijk, pk -> ijp", p0_proj_to_im1, intrinsic1)
        p1_norm_im0 = torch.einsum("ijk, pk -> ijp", p1_proj_to_im0, intrinsic0)

        p0_norm_im1_2d = p0_norm_im1[:, :, :2] / (p0_norm_im1[:, :, 2, None] + 1e-10)
        p1_norm_im0_2d = p1_norm_im0[:, :, :2] / (p1_norm_im0[:, :, 2, None] + 1e-10)
        return p1_norm_im0_2d[0], p0_norm_im1_2d[0], valid
        #####


    if projection_type == 'average':
        # project avg of 3d points to target image
        #####
        avg_proj_to_im0 = torch.einsum("ijk, pk -> ijp", avg_p0_p1, w2c0)
        avg_proj_to_im1 = torch.einsum("ijk, pk -> ijp", avg_p0_p1, w2c1)
        avg_norm_im0 = torch.einsum("ijk, pk -> ijp", avg_proj_to_im0, intrinsic0)
        avg_norm_im1 = torch.einsum("ijk, pk -> ijp", avg_proj_to_im1, intrinsic1)
        avg_norm_im0_2d = avg_norm_im0[:, :, :2] / (avg_norm_im0[:, :, 2, None] + 1e-10)
        avg_norm_im1_2d = avg_norm_im1[:, :, :2] / (avg_norm_im1[:, :, 2, None] + 1e-10)
        #####
        return avg_norm_im0_2d[0], avg_norm_im1_2d[0], valid

def lines_intersect(origin0, origin1, direction0, direction1):
    """
    origin: [1, number_of_points, 3]
    direction: [1, number_of_points, 3]
    return: [1, number_of_points, 4] homogenous xyz1
    """
    r0_r1 = torch.einsum(
        "ijk, ijk -> ij",
        direction0,
        direction1
    )
    t0 = (
        torch.einsum(
            "ijk, ijk -> ij",
            direction0,
            origin0 - origin1
        ) - r0_r1
        * torch.einsum(
            "ijk, ijk -> ij",
            direction1,
            origin0 - origin1
        )
    ) / (r0_r1 ** 2 - 1 + 1e-10)

    t1 = (
        torch.einsum(
            "ijk, ijk -> ij",
            direction1,
            origin1 - origin0
        ) - r0_r1
        * torch.einsum(
            "ijk, ijk -> ij",
            direction0,
            origin1 - origin0
        )
    ) / (r0_r1 ** 2 - 1 + 1e-10)

    p0 = t0[:, :, None] * direction0 + origin0
    p1 = t1[:, :, None] * direction1 + origin1
    p0_4d = torch.cat(
        [p0, torch.ones((p0.shape[:2]), device='cuda')[:, :, None]], dim=-1
    )
    p1_4d = torch.cat(
        [p1, torch.ones((p1.shape[:2]), device='cuda')[:, :, None]], dim=-1
    )

    # Chirality check: remove rays behind cameras
    # Find indices of valid rays
    valid_t0 = (t0 > 0).flatten()
    valid_t1 = (t1 > 0).flatten()
    valid = torch.logical_and(valid_t0, valid_t1)
    return p0_4d, p1_4d, valid

def light_glue_simple(image0, image1, features='superpoint'):
    if features == 'superpoint':
        extractor = SuperPoint(max_num_keypoints=512).eval().cuda()  # load the extractor
    if features == 'disk':
        extractor = DISK(max_num_keypoints=512).eval().cuda()  # load the extractor
    if features == 'aliked':
        extractor = ALIKED(max_num_keypoints=512).eval().cuda()  # load the extractor
    if features == 'sift':
        extractor = SIFT(max_num_keypoints=512).eval().cuda()  # load the extractor
    matcher = LightGlue(features=features, depth_confidence=0.9, width_confidence=0.95).eval().cuda()  # load the matcher

    # extract local features
    feats0 = extractor.extract(image0)  # auto-resize the image, disable with resize=None
    feats1 = extractor.extract(image1)

    # match the features
    matches01 = matcher({'image0': feats0, 'image1': feats1})
    feats0, feats1, matches01 = [rbd(x) for x in [feats0, feats1, matches01]]  # remove batch dimension
    kpts0, kpts1, matches = feats0["keypoints"], feats1["keypoints"], matches01["matches"]
    m_kpts0, m_kpts1 = kpts0[matches[..., 0]], kpts1[matches[..., 1]]

    return m_kpts0, m_kpts1

def light_glue(image0, image1, features='superpoint'):
    if features == 'superpoint':
        extractor = SuperPoint(max_num_keypoints=2048).eval().cuda()  # load the extractor
    if features == 'disk':
        extractor = DISK(max_num_keypoints=2048).eval().cuda()  # load the extractor
    if features == 'aliked':
        extractor = ALIKED(max_num_keypoints=2048).eval().cuda()  # load the extractor
    if features == 'sift':
        extractor = SIFT(max_num_keypoints=2048).eval().cuda()  # load the extractor
    matcher = LightGlue(features=features).eval().cuda()  # load the matcher

    # extract local features
    feats0 = extractor.extract(image0)  # auto-resize the image, disable with resize=None
    feats1 = extractor.extract(image1)

    # match the features
    matches01 = matcher({'image0': feats0, 'image1': feats1})
    feats0, feats1, matches01 = [rbd(x) for x in [feats0, feats1, matches01]]  # remove batch dimension
    kpts0, kpts1, matches = feats0["keypoints"], feats1["keypoints"], matches01["matches"]
    m_kpts0, m_kpts1 = kpts0[matches[..., 0]], kpts1[matches[..., 1]]

    axes = viz2d.plot_images([image0, image1])
    viz2d.plot_matches(m_kpts0, m_kpts1, color="lime", lw=0.2)
    viz2d.add_text(0, f'Stop after {matches01["stop"]} layers', fs=20)
    first_plot = viz2d.get_plot()

    kpc0, kpc1 = viz2d.cm_prune(matches01["prune0"]), viz2d.cm_prune(matches01["prune1"])
    viz2d.plot_images([image0, image1])
    viz2d.plot_keypoints([kpts0, kpts1], colors=[kpc0, kpc1], ps=10)
    second_plot = viz2d.get_plot()
    return first_plot, second_plot, m_kpts0, m_kpts1

def prepare_output_and_logger(args):
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])

    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()},
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()


def init_wandb(cfg, wandb_id=None, project="", run_name=None, mode="online", resume=False, use_group=False, set_group=None):
    r"""Initialize Weights & Biases (wandb) logger.

    Args:
        cfg (obj): Global configuration.
        wandb_id (str): A unique ID for this run, used for resuming.
        project (str): The name of the project where you're sending the new run.
            If the project is not specified, the run is put in an "Uncategorized" project.
        run_name (str): name for each wandb run (useful for logging changes)
        mode (str): online/offline/disabled
    """
    print('Initialize wandb')
    if not wandb_id:
        wandb_path = os.path.join(cfg.model_path, "wandb_id.txt")
        if resume and os.path.exists(wandb_path):
            with open(wandb_path, "r") as f:
                wandb_id = f.read()
        else:
            wandb_id = wandb.util.generate_id()
            with open(wandb_path, "w") as f:
                f.write(wandb_id)
    if use_group:
        group, name = cfg.model_path.split("/")[-2:]
        group = set_group
    else:
        group, name = None, os.path.basename(cfg.model_path)
        group = set_group

    if run_name is not None:
        name = run_name
    wandb.init(id=wandb_id,
               project=project,
               config=vars(cfg),
               group=group,
               name=name,
               dir=cfg.model_path,
               resume=resume,
               settings=wandb.Settings(start_method="fork"),
               mode=mode)
    wandb.config.update({'dataset': cfg.source_path})

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    # wandb setting
    parser.add_argument("--wandb", action="store_true", default=False)
    parser.add_argument("--wandb_project_name", type=str, default = None)
    parser.add_argument("--wandb_group_name", type=str, default = None)
    parser.add_argument("--wandb_mode", type=str, default = "online")
    parser.add_argument("--resume", action="store_true", default=False)
    # random init point cloud
    parser.add_argument("--random_init_pc", action="store_true", default=False)

    # use hybrid for specular
    parser.add_argument("--hybrid", action="store_true", default=False)
    # if optimize camera poses
    parser.add_argument("--opt_cam", action="store_true", default=False)
    # noise for rotation and translation
    parser.add_argument("--r_t_noise", nargs="+", type=float, default=[0., 0.])

    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)
    print("Optimizing " + args.model_path)

    # Initialize wandb
    if args.wandb:
        wandb.login()
        wandb_run = init_wandb(args,
                               project=args.wandb_project_name,
                               mode=args.wandb_mode,
                               resume=args.resume,
                               use_group=True,
                               set_group=args.wandb_group_name
                               )

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    training(lp.extract(args), op.extract(args), pp.extract(args), args.test_iterations, args.save_iterations, args.checkpoint_iterations, args.start_checkpoint, args.debug_from, use_wandb=args.wandb, random_init=args.random_init_pc, hybrid=args.hybrid, opt_cam=args.opt_cam, r_t_noise=args.r_t_noise)

    # All done
    print("\nTraining complete.")
