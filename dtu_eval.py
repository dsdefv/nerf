import sys,os,imageio,lpips
# root = '/home/hengfei/Desktop/research/mvsnerf'
# os.chdir(root)
# sys.path.append(root)

from opt import config_parser
from data import dataset_dict
from torch.utils.data import DataLoader
import matplotlib.pyplot as plt


# models
from models import *
from renderer import *
from data.ray_utils import get_rays

from tqdm import tqdm


from skimage.metrics import structural_similarity

# pytorch-lightning
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningModule, Trainer, loggers


from data.ray_utils import ray_marcher

# %load_ext autoreload
# %autoreload 2

torch.cuda.set_device(1)
os.environ['CUDA_VISIBLE_DEVICES'] = '1'

def decode_batch(batch):
    rays = batch['rays']  # (B, 8)
    rgbs = batch['rgbs']  # (B, 3)
    return rays, rgbs

def unpreprocess(data, shape=(1,1,3,1,1)):
    # to unnormalize image for visualization
    # data N V C H W
    device = data.device
    mean = torch.tensor([-0.485 / 0.229, -0.456 / 0.224, -0.406 / 0.225]).view(*shape).to(device)
    std = torch.tensor([1 / 0.229, 1 / 0.224, 1 / 0.225]).view(*shape).to(device)

    return (data - mean) / std

def read_depth(filename):
    depth_h = np.array(read_pfm(filename)[0], dtype=np.float32) # (800, 800)
    depth_h = cv2.resize(depth_h, None, fx=0.5, fy=0.5,
                       interpolation=cv2.INTER_NEAREST)  # (600, 800)
    depth_h = depth_h[44:556, 80:720]  # (512, 640)
#     depth = cv2.resize(depth_h, None, fx=0.5, fy=0.5,interpolation=cv2.INTER_NEAREST)#!!!!!!!!!!!!!!!!!!!!!!!!!
    mask = depth>0
    return depth_h,mask

loss_fn_vgg = lpips.LPIPS(net='vgg')
mse2psnr = lambda x : -10. * np.log(x) / np.log(10.)





psnr_all, ssim_all, LPIPS_vgg_all = [], [], []
depth_acc = {}
eval_metric = [0.1, 0.05, 0.01]
depth_acc[f'abs_err'], depth_acc[f'acc_l_{eval_metric[0]}'], depth_acc[f'acc_l_{eval_metric[1]}'], depth_acc[
    f'acc_l_{eval_metric[2]}'] = {}, {}, {}, {}
for i_scene, scene in enumerate([1, 8, 21, 103, 114]):  # ,8,21,103,114
    psnr, ssim, LPIPS_vgg = [], [], []
    cmd = f'--datadir DTU/mvs_training/dtu/scan{scene}  \
     --dataset_name dtu_ft  \
     --net_type v0 --ckpt ./ckpts/mvsnerf-v0.tar '

    args = config_parser(cmd.split())
    args.use_viewdirs = True

    args.N_samples = 128
    args.feat_dim = 8 + 12

    # create models
    if 0 == i_scene:
        render_kwargs_train, render_kwargs_test, start, grad_vars = create_nerf_mvs(args, use_mvs=True,
                                                                                    dir_embedder=False,
                                                                                    pts_embedder=True)
        filter_keys(render_kwargs_train)

        MVSNet = render_kwargs_train['network_mvs']
        render_kwargs_train.pop('network_mvs')

    datadir = args.datadir
    datatype = 'train'
    pad = 16
    args.chunk = 5120

    print('============> rendering dataset <===================')
    dataset_train = dataset_dict[args.dataset_name](args, split='train')
    dataset_val = dataset_dict[args.dataset_name](args, split='val')
    val_idx = dataset_val.img_idx

    save_as_image = True
    save_dir = f'results/test3'
    os.makedirs(save_dir, exist_ok=True)
    MVSNet.train()
    MVSNet = MVSNet.cuda()

    with torch.no_grad():

        try:
            tqdm._instances.clear()
        except Exception:
            pass

        for i, batch in enumerate(tqdm(dataset_val)):
            torch.cuda.empty_cache()

            rays, img = decode_batch(batch)
            rays = rays.squeeze().to(device)  # (H*W, 3)
            img = img.squeeze().cpu().numpy()  # (H, W, 3)
            depth = batch['depth'].squeeze().numpy()  # (H, W)

            # find nearest image idx from training views
            positions = dataset_train.poses[:, :3, 3]
            dis = np.sum(np.abs(positions - dataset_val.poses[[i], :3, 3]), axis=-1)
            pair_idx = np.argsort(dis)[:3]
            pair_idx = [dataset_train.img_idx[item] for item in pair_idx]

            imgs_source, proj_mats, near_far_source, pose_source = dataset_train.read_source_views(pair_idx=pair_idx,
                                                                                                   device=device)
            volume_feature, _, _ = MVSNet(imgs_source, proj_mats, near_far_source, pad=pad)
            imgs_source = unpreprocess(imgs_source)

            N_rays_all = rays.shape[0]
            rgb_rays, depth_rays_preds = [], []
            for chunk_idx in range(N_rays_all // args.chunk + int(N_rays_all % args.chunk > 0)):
                xyz_coarse_sampled, rays_o, rays_d, z_vals = ray_marcher(
                    rays[chunk_idx * args.chunk:(chunk_idx + 1) * args.chunk],
                    N_samples=args.N_samples)

                # Converting world coordinate to ndc coordinate
                H, W = img.shape[:2]
                inv_scale = torch.tensor([W - 1, H - 1]).to(device)
                w2c_ref, intrinsic_ref = pose_source['w2cs'][0], pose_source['intrinsics'][0].clone()
                xyz_NDC = get_ndc_coordinate(w2c_ref, intrinsic_ref, xyz_coarse_sampled, inv_scale,
                                             near=near_far_source[0], far=near_far_source[1],
                                             pad=pad * args.imgScale_test)

                # rendering
                rgb, disp, acc, depth_pred, alpha, extras = rendering(args, pose_source, xyz_coarse_sampled,
                                                                      xyz_NDC, z_vals, rays_o, rays_d,
                                                                      volume_feature, imgs_source,
                                                                      **render_kwargs_train)

                rgb, depth_pred = torch.clamp(rgb.cpu(), 0, 1.0).numpy(), depth_pred.cpu().numpy()
                rgb_rays.append(rgb)
                depth_rays_preds.append(depth_pred)

            depth_rays_preds = np.concatenate(depth_rays_preds).reshape(H, W)

            depth_gt, _ = read_depth(
                f'DTU/mvs_training/dtu/Depths/scan{scene}/depth_map_{val_idx[i]:04d}.pfm')

            mask_gt = depth_gt > 0
            abs_err = abs_error(depth_rays_preds, depth_gt / 200, mask_gt)

            eval_metric = [0.01, 0.05, 0.1]
            depth_acc[f'abs_err'][f'{scene}'] = np.mean(abs_err)
            depth_acc[f'acc_l_{eval_metric[0]}'][f'{scene}'] = acc_threshold(abs_err, eval_metric[0]).mean()
            depth_acc[f'acc_l_{eval_metric[1]}'][f'{scene}'] = acc_threshold(abs_err, eval_metric[1]).mean()
            depth_acc[f'acc_l_{eval_metric[2]}'][f'{scene}'] = acc_threshold(abs_err, eval_metric[2]).mean()

            depth_rays_preds, _ = visualize_depth_numpy(depth_rays_preds, near_far_source)

            rgb_rays = np.concatenate(rgb_rays).reshape(H, W, 3)
            img_vis = np.concatenate((img * 255, rgb_rays * 255, depth_rays_preds), axis=1)

            if save_as_image:
                imageio.imwrite(f'{save_dir}/scan{scene}_{val_idx[i]:03d}.png', img_vis.astype('uint8'))
            else:
                rgbs.append(img_vis.astype('uint8'))

            # quantity
            # mask background since they are outside the far boundle
            mask = depth == 0
            imageio.imwrite(f'{save_dir}/scan{scene}_{val_idx[i]:03d}_mask.png', mask.astype('uint8') * 255)
            rgb_rays[mask], img[mask] = 0.0, 0.0
            psnr.append(mse2psnr(np.mean((rgb_rays[~mask] - img[~mask]) ** 2)))
            ssim.append(structural_similarity(rgb_rays, img, multichannel=True))

            img_tensor = torch.from_numpy(rgb_rays)[None].permute(0, 3, 1,
                                                                  2).float() * 2 - 1.0  # image should be RGB, IMPORTANT: normalized to [-1,1]
            img_gt_tensor = torch.from_numpy(img)[None].permute(0, 3, 1, 2).float() * 2 - 1.0
            LPIPS_vgg.append(loss_fn_vgg(img_tensor, img_gt_tensor).item())

        print(f'=====> scene: {scene} mean psnr {np.mean(psnr)} ssim: {np.mean(ssim)} lpips: {np.mean(LPIPS_vgg)}')
        psnr_all.append(psnr);
        ssim_all.append(ssim);
        LPIPS_vgg_all.append(LPIPS_vgg)

    if not save_as_image:
        imageio.mimwrite(f'{save_dir}/{scene}_spiral.mp4', np.stack(rgbs), fps=20, quality=10)

a = np.mean(list(depth_acc['abs_err'].values()))
b = np.mean(list(depth_acc[f'acc_l_{eval_metric[0]}'].values()))
c = np.mean(list(depth_acc[f'acc_l_{eval_metric[1]}'].values()))
d = np.mean(list(depth_acc[f'acc_l_{eval_metric[2]}'].values()))
print(f'============> abs_err: {a} <=================')
print(f'============> acc_l_{eval_metric[0]}: {b} <=================')
print(f'============> acc_l_{eval_metric[1]}: {c} <=================')
print(f'============> acc_l_{eval_metric[2]}: {d} <=================')
print(f'=====> all mean psnr {np.mean(psnr_all)} ssim: {np.mean(ssim_all)} lpips: {np.mean(LPIPS_vgg_all)}')