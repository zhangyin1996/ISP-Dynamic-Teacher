import torch
import torchvision.transforms as T
import numpy as np
from numpy import random as R
import cv2
import random
import scipy.stats as stats

def random_noise_levels():
    """Generates random shot and read noise from a log-log linear distribution."""
    log_min_shot_noise = np.log(0.0001)
    log_max_shot_noise = np.log(0.012)
    log_shot_noise = np.random.uniform(log_min_shot_noise, log_max_shot_noise)
    shot_noise = np.exp(log_shot_noise)

    line = lambda x: 2.18 * x + 1.20
    log_read_noise = line(log_shot_noise) + np.random.normal(scale=0.26)
    # print('shot noise and read noise:', log_shot_noise, log_read_noise)
    read_noise = np.exp(log_read_noise)
    return shot_noise, read_noise

class NightAug:
    def __init__(self):
        self.gaussian = T.GaussianBlur(11,(0.1,2.0))
    def mask_img(self,img,cln_img):
        while R.random()>0.4:
            x1 = R.randint(img.shape[1])
            x2 = R.randint(img.shape[1])
            y1 = R.randint(img.shape[2])
            y2 = R.randint(img.shape[2])
            img[:,x1:x2,y1:y2]=cln_img[:,x1:x2,y1:y2]
        return img

    def gaussian_heatmap(self,x):
        """
        It produces single gaussian at a random point
        """
        sig = torch.randint(low=1,high=150,size=(1,)).cuda()[0]
        image_size = x.shape[1:]
        center = (torch.randint(image_size[0],(1,))[0].cuda(), torch.randint(image_size[1],(1,))[0].cuda())
        x_axis = torch.linspace(0, image_size[0]-1, image_size[0]).cuda() - center[0]
        y_axis = torch.linspace(0, image_size[1]-1, image_size[1]).cuda() - center[1]
        xx, yy = torch.meshgrid(x_axis, y_axis)
        kernel = torch.exp(-0.5 * (torch.square(xx) + torch.square(yy)) / torch.square(sig))
        new_img = (x*(1-kernel) + 255*kernel).type(torch.uint8)
        return new_img

    def aug(self,x):
        for sample in x:
            img = sample['image'].cuda()
            g_b_flag = True

            # Guassian Blur
            if R.random()>0.5:
                img = self.gaussian(img)
            
            cln_img_zero = img.detach().clone()

            # Gamma
            if R.random()>0.5:
                cln_img = img.detach().clone()
                val = 1/(R.random()*0.8+0.2)
                img = T.functional.adjust_gamma(img,val)
                img= self.mask_img(img,cln_img)
                g_b_flag = False
            
            # Brightness
            if R.random()>0.5 or g_b_flag:
                cln_img = img.detach().clone()
                val = R.random()*0.8+0.2
                img = T.functional.adjust_brightness(img,val)
                img= self.mask_img(img,cln_img)

            # Contrast
            if R.random()>0.5:
                cln_img = img.detach().clone()
                val = R.random()*0.8+0.2
                img = T.functional.adjust_contrast(img,val)
                img= self.mask_img(img,cln_img)
            img= self.mask_img(img,cln_img_zero)

            prob = 0.5
            while R.random()>prob:
                img=self.gaussian_heatmap(img)
                prob+=0.1

            #Noise
            if R.random()>0.5:
                n = torch.clamp(torch.normal(0,R.randint(50),img.shape),min=0).cuda()
                img = n + img
                img = torch.clamp(img,max = 255).type(torch.uint8)

            sample['image'] = img.cpu()
        return x

    def apply_ccm(self, image, ccm):
        '''
        The function of apply CCM matrix
        '''
        shape = image.shape
        image = image.view(-1, 3)
        image = torch.tensordot(image, ccm, dims=[[-1], [-1]])
        return image.view(shape)

    def Low_Illumination_Degrading(self, img, safe_invert=False):    # ZY: 论文3.3.2

        '''
        (1)unprocess part(RGB2RAW) (2)low light corruption part (3)ISP part(RAW2RGB)
        Some code copy from 'https://github.com/timothybrooks/unprocessing', thx to their work ~
        input:
        img (Tensor): Input normal light images of shape (C, H, W).
        img_meta(dict): A image info dict contain some information like name ,shape ...
        return:
        img_deg (Tensor): Output degration low light images of shape (C, H, W).
        degration_info(Tensor): Output degration paramter in the whole process.
        '''

        '''
        parameter setting
        '''
        device = img.device
        #config = self.degration_cfg

        # ZY: transformation_para
        transformation_para = {}
        transformation_para['darkness_range'] = (0.01, 1.0)
        transformation_para['gamma_range'] = (2.0, 3.5)
        transformation_para['rgb_range'] = (0.8, 0.1)
        transformation_para['red_range'] = (1.9, 2.4)
        transformation_para['blue_range'] = (1.5, 1.9)
        transformation_para['quantisation'] = [12, 14, 16]

        # camera color matrix
        xyz2cams = [[[1.0234, -0.2969, -0.2266],
                     [-0.5625, 1.6328, -0.0469],
                     [-0.0703, 0.2188, 0.6406]],
                    [[0.4913, -0.0541, -0.0202],
                     [-0.613, 1.3513, 0.2906],
                     [-0.1564, 0.2151, 0.7183]],
                    [[0.838, -0.263, -0.0639],
                     [-0.2887, 1.0725, 0.2496],
                     [-0.0627, 0.1427, 0.5438]],
                    [[0.6596, -0.2079, -0.0562],
                     [-0.4782, 1.3016, 0.1933],
                     [-0.097, 0.1581, 0.5181]]]
        rgb2xyz = [[0.4124564, 0.3575761, 0.1804375],
                   [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]]

        # noise parameters and quantization step

        '''
        (1)unprocess part(RGB2RAW): 1.inverse tone, 2.inverse gamma, 3.sRGB2cRGB, 4.inverse WB digital gains
        '''
        img1 = img.permute(1, 2, 0)  # (C, H, W) -- (H, W, C)
        # print(img1.shape)
        # img_meta = img_metas[i]
        # inverse tone mapping
        img1 = img1/255.0    # ZY: 不进行归一化的化，下面的arcsin函数会报错，因为arcsin的定义域是(-1, 1)
        img1 = 0.5 - torch.sin(torch.asin(1.0 - 2.0 * img1) / 3.0)    # ZY: 论文公式(13)
        # inverse gamma
        epsilon = torch.FloatTensor([1e-8]).to(torch.device(device))
        gamma = random.uniform(transformation_para['gamma_range'][0], transformation_para['gamma_range'][1])
        img2 = torch.max(img1, epsilon) ** gamma    # ZY: 论文公式(11)
        # sRGB2cRGB
        xyz2cam = random.choice(xyz2cams)
        rgb2cam = np.matmul(xyz2cam, rgb2xyz)
        rgb2cam = torch.from_numpy(rgb2cam / np.sum(rgb2cam, axis=-1)).to(torch.float).to(torch.device(device))
        # print(rgb2cam)
        img3 = self.apply_ccm(img2, rgb2cam)    # ZY: 论文公式(9)
        # img3 = torch.clamp(img3, min=0.0, max=1.0)

        # inverse WB
        rgb_gain = random.normalvariate(transformation_para['rgb_range'][0], transformation_para['rgb_range'][1])
        red_gain = random.uniform(transformation_para['red_range'][0], transformation_para['red_range'][1])    # ZY: g_r
        blue_gain = random.uniform(transformation_para['blue_range'][0], transformation_para['blue_range'][1])    # ZY: g_b

        gains1 = np.stack([1.0 / red_gain, 1.0, 1.0 / blue_gain]) * rgb_gain
        # gains1 = np.stack([1.0 / red_gain, 1.0, 1.0 / blue_gain])
        gains1 = gains1[np.newaxis, np.newaxis, :]
        gains1 = torch.FloatTensor(gains1).to(torch.device(device))

        # color disorder !!!
        if safe_invert:
            img3_gray = torch.mean(img3, dim=-1, keepdim=True)
            inflection = 0.9
            zero = torch.zeros_like(img3_gray).to(torch.device(device))
            mask = (torch.max(img3_gray - inflection, zero) / (1.0 - inflection)) ** 2.0
            safe_gains = torch.max(mask + (1.0 - mask) * gains1, gains1)

            #img4 = img3 * gains1
            img4 = torch.clamp(img3*safe_gains, min=0.0, max=1.0)

        else:
            img4 = img3 * gains1

        '''
        (2)low light corruption part: 5.darkness, 6.shot and read noise 
        '''
        # darkness(low photon numbers)
        lower, upper = transformation_para['darkness_range'][0], transformation_para['darkness_range'][1]
        mu, sigma = 0.1, 0.08
        darkness = stats.truncnorm((lower - mu) / sigma, (upper - mu) / sigma, loc=mu, scale=sigma)    # ZY: 论文公式(14) k
        darkness = darkness.rvs()
        # print(darkness)
        img5 = img4 * darkness    # ZY: 论文公式(14) kx
        # add shot and read noise
        shot_noise, read_noise = random_noise_levels()
        var = img5 * shot_noise + read_noise  # here the read noise is independent    # ZY: 论文公式(14) xigema^2
        var = torch.max(var, epsilon)
        # print('the var is:', var)
        noise = torch.normal(mean=0, std=torch.sqrt(var))
        img6 = img5 + noise

        '''
        (3)ISP part(RAW2RGB): 7.quantisation  8.white balance 9.cRGB2sRGB 10.gamma correction
        '''
        # quantisation noise: uniform distribution
        bits = random.choice(transformation_para['quantisation'])    # ZY: 论文公式(6) B
        quan_noise = torch.FloatTensor(img6.size()).uniform_(-1 / (255 * bits), 1 / (255 * bits)).to(
            torch.device(device))    # ZY: 论文公式(6)
        # print(quan_noise)
        # img7 = torch.clamp(img6 + quan_noise, min=0)
        img7 = img6 + quan_noise    # ZY: 论文公式(6)
        # white balance
        gains2 = np.stack([red_gain, 1.0, blue_gain])
        gains2 = gains2[np.newaxis, np.newaxis, :]
        gains2 = torch.FloatTensor(gains2).to(torch.device(device))
        img8 = img7 * gains2    # ZY: 论文公式(7)
        # cRGB2sRGB
        cam2rgb = torch.inverse(rgb2cam)
        img9 = self.apply_ccm(img8, cam2rgb)    # ZY: 论文公式(8)
        # gamma correction
        img10 = torch.max(img9, epsilon) ** (1 / gamma)    # ZY: 论文公式(10)



        img_low = img10.permute(2, 0, 1)  # (H, W, C) -- (C, H, W)
        img_low = img_low * 255.0    # ZY: 还原回去
        # degration infomations: darkness, gamma value, WB red, WB blue
        # dark_gt = torch.FloatTensor([darkness]).to(torch.device(device))
        para_gt = torch.FloatTensor([darkness, 1.0 / gamma, 1.0 / red_gain, 1.0 / blue_gain]).to(torch.device(device))    # ZY [???]
        # others_gt = torch.FloatTensor([1.0 / gamma, 1.0, 1.0]).to(torch.device(device))
        # print('the degration information:', degration_info)
        return img_low, para_gt