import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.architectures.video import warp

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1, mode=None):
    if mode == 'rife':
        return nn.Sequential(
            nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                    padding=padding, dilation=dilation, bias=True),
            nn.PReLU(out_planes)
        )
    elif mode == 'ifnet':
        return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=False),
        nn.BatchNorm2d(out_planes),
        nn.PReLU(out_planes)
        )

def deconv(in_planes, out_planes, kernel_size=4, stride=2, padding=1):
    return nn.Sequential(
        torch.nn.ConvTranspose2d(in_channels=in_planes, out_channels=out_planes,
                                 kernel_size=4, stride=2, padding=1, bias=True),
        nn.PReLU(out_planes)
    )

def conv_woact(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1, mode=None):
    if mode == 'rife':
        return nn.Sequential(
            nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                    padding=padding, dilation=dilation, bias=True),
        )
    elif mode == 'ifnet':
        return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=False),
        nn.BatchNorm2d(out_planes),
        )

class ResBlock(nn.Module):
    def __init__(self, in_planes, out_planes, stride, mode=None):
        super(ResBlock, self).__init__()
        if in_planes == out_planes and stride == 1:
            self.conv0 = nn.Identity()
        else:
            self.conv0 = nn.Conv2d(in_planes, out_planes,
                                   3, stride, 1, bias=False)
        if mode == 'rife':
            self.conv1 = conv(in_planes, out_planes, 3, stride, 1, mode=mode)
        elif mode == 'ifnet':
            self.conv1 = conv(in_planes, out_planes, 5, stride, 2, mode=mode)
        self.conv2 = conv_woact(out_planes, out_planes, 3, 1, 1, mode=mode)
        self.relu1 = nn.PReLU(1)
        self.relu2 = nn.PReLU(out_planes)
        self.fc1 = nn.Conv2d(out_planes, 16, kernel_size=1, bias=False)
        self.fc2 = nn.Conv2d(16, out_planes, kernel_size=1, bias=False)

    def forward(self, x):
        y = self.conv0(x)
        x = self.conv1(x)
        x = self.conv2(x)
        w = x.mean(3, True).mean(2, True)
        w = self.relu1(self.fc1(w))
        w = torch.sigmoid(self.fc2(w))
        x = self.relu2(x * w + y)
        return x


class IFBlock(nn.Module):
    def __init__(self, in_planes, scale=1, c=64):
        super(IFBlock, self).__init__()
        self.scale = scale
        self.conv0 = conv(in_planes, c, 5, 2, 2, 1, mode='ifnet')
        self.res0 = ResBlock(c, c, 1, mode='ifnet')
        self.res1 = ResBlock(c, c, 1, mode='ifnet')
        self.res2 = ResBlock(c, c, 1, mode='ifnet')
        self.res3 = ResBlock(c, c, 1, mode='ifnet')
        self.res4 = ResBlock(c, c, 1, mode='ifnet')
        self.res5 = ResBlock(c, c, 1, mode='ifnet')
        self.conv1 = nn.Conv2d(c, 8, 3, 1, 1)
        self.up = nn.PixelShuffle(2)

    def forward(self, x):
        if self.scale != 1:
            x = F.interpolate(x, scale_factor=1. / self.scale, mode="bilinear",
                              align_corners=False)
        x = self.conv0(x)
        x = self.res0(x)
        x = self.res1(x)
        x = self.res2(x)
        x = self.res3(x)
        x = self.res4(x)
        x = self.res5(x)
        x = self.conv1(x)
        flow = self.up(x)
        if self.scale != 1:
            flow = F.interpolate(flow, scale_factor=self.scale, mode="bilinear",
                                 align_corners=False)
        return flow


class IFNet(nn.Module):
    def __init__(self):
        super(IFNet, self).__init__()
        self.block0 = IFBlock(6, scale=8, c=192)
        self.block1 = IFBlock(8, scale=4, c=128)
        self.block2 = IFBlock(8, scale=2, c=96)
        self.block3 = IFBlock(8, scale=1, c=48)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=0.5, mode="bilinear",
                          align_corners=False)
        flow0 = self.block0(x)
        F1 = flow0
        warped_img0 = warp(x[:, :3], F1)
        warped_img1 = warp(x[:, 3:], -F1)
        flow1 = self.block1(torch.cat((warped_img0, warped_img1, F1), 1))
        F2 = (flow0 + flow1)
        warped_img0 = warp(x[:, :3], F2)
        warped_img1 = warp(x[:, 3:], -F2)
        flow2 = self.block2(torch.cat((warped_img0, warped_img1, F2), 1))
        F3 = (flow0 + flow1 + flow2)
        warped_img0 = warp(x[:, :3], F3)
        warped_img1 = warp(x[:, 3:], -F3)
        flow3 = self.block3(torch.cat((warped_img0, warped_img1, F3), 1))
        F4 = (flow0 + flow1 + flow2 + flow3)
        return F4, [F1, F2, F3, F4]

class ContextNet(nn.Module):
    def __init__(self, c = 32):
        super(ContextNet, self).__init__()
        self.conv0 = conv(3, c, 3, 2, 1, mode='rife')
        self.conv1 = ResBlock(c, c, 2, mode='rife')
        self.conv2 = ResBlock(c, 2*c, 2, mode='rife')
        self.conv3 = ResBlock(2*c, 4*c, 2, mode='rife')
        self.conv4 = ResBlock(4*c, 8*c, 2, mode='rife')

    def forward(self, x, flow):
        x = self.conv0(x)
        x = self.conv1(x)
        flow = F.interpolate(flow, scale_factor=0.5, mode="bilinear", align_corners=False, recompute_scale_factor=False) * 0.5
        f1 = warp(x, flow)
        x = self.conv2(x)
        flow = F.interpolate(flow, scale_factor=0.5, mode="bilinear",
                             align_corners=False) * 0.5
        f2 = warp(x, flow)
        x = self.conv3(x)
        flow = F.interpolate(flow, scale_factor=0.5, mode="bilinear",
                             align_corners=False) * 0.5
        f3 = warp(x, flow)
        x = self.conv4(x)
        flow = F.interpolate(flow, scale_factor=0.5, mode="bilinear",
                             align_corners=False) * 0.5
        f4 = warp(x, flow)
        return [f1, f2, f3, f4]


class FusionNet(nn.Module):
    def __init__(self, c = 32):
        super(FusionNet, self).__init__()
        self.conv0 = conv(8, c, 3, 2, 1, mode='rife')
        self.down0 = ResBlock(c, 2*c, 2, mode='rife')
        self.down1 = ResBlock(4*c, 4*c, 2, mode='rife')
        self.down2 = ResBlock(8*c, 8*c, 2, mode='rife')
        self.down3 = ResBlock(16*c, 16*c, 2, mode='rife')
        self.up0 = deconv(32*c, 8*c)
        self.up1 = deconv(16*c, 4*c)
        self.up2 = deconv(8*c, 2*c)
        self.up3 = deconv(4*c, c)
        self.conv = nn.Conv2d(c, 16, 3, 1, 1)
        self.up4 = nn.PixelShuffle(2)

    def forward(self, img0, img1, flow, c0, c1, flow_gt):
        warped_img0 = warp(img0, flow)
        warped_img1 = warp(img1, -flow)
        if flow_gt == None:
            warped_img0_gt, warped_img1_gt = None, None
        else:
            warped_img0_gt = warp(img0, flow_gt[:, :2])
            warped_img1_gt = warp(img1, flow_gt[:, 2:4])
        x = self.conv0(torch.cat((warped_img0, warped_img1, flow), 1))
        s0 = self.down0(x)
        s1 = self.down1(torch.cat((s0, c0[0], c1[0]), 1))
        s2 = self.down2(torch.cat((s1, c0[1], c1[1]), 1))
        s3 = self.down3(torch.cat((s2, c0[2], c1[2]), 1))
        x = self.up0(torch.cat((s3, c0[3], c1[3]), 1))
        x = self.up1(torch.cat((x, s2), 1))
        x = self.up2(torch.cat((x, s1), 1))
        x = self.up3(torch.cat((x, s0), 1))
        x = self.up4(self.conv(x))
        return x, warped_img0, warped_img1, warped_img0_gt, warped_img1_gt


class RIFE(nn.Module):
    def __init__(self, device='cpu'):
        super(RIFE, self).__init__()
        self.flownet = IFNet().eval().to(device)
        self.contextnet = ContextNet().eval().to(device)
        self.fusionnet = FusionNet().eval().to(device)\

    def forward(self, img0=None, img1=None, imgs=None, flow=None, training=True, flow_gt=None):
        imgs = torch.cat((img0, img1), 1)
        flow, _ = self.flownet(imgs)
        img0 = imgs[:, :3]
        img1 = imgs[:, 3:]
        c0 = self.contextnet(img0, flow)
        c1 = self.contextnet(img1, -flow)
        flow = F.interpolate(flow, scale_factor=2.0, mode="bilinear",
                             align_corners=False) * 2.0
        refine_output, warped_img0, warped_img1, warped_img0_gt, warped_img1_gt = self.fusionnet(
            img0, img1, flow, c0, c1, flow_gt)
        res = torch.sigmoid(refine_output[:, :3]) * 2 - 1
        mask = torch.sigmoid(refine_output[:, 3:4])
        merged_img = warped_img0 * mask + warped_img1 * (1 - mask)
        pred = merged_img + res
        pred = torch.clamp(pred, 0, 1)
        if training:
            return pred, mask, merged_img, warped_img0, warped_img1, warped_img0_gt, warped_img1_gt
        else:
            return pred