import os
from skimage import color
import cv2
import argparse
import numpy as np
import torch.nn.functional as F
from tqdm import tqdm
import torch
import torch.nn as nn
from basicsr.archs.ddcolor_arch_utils.unet import Hook, CustomPixelShuffle_ICNR, UnetBlockWide, NormType, \
    custom_conv_layer
from basicsr.archs.ddcolor_arch_utils.convnext import ConvNeXt
from basicsr.archs.ddcolor_arch_utils.transformer_utils import SelfAttentionLayer, CrossAttentionLayer, FFNLayer, MLP
from basicsr.archs.ddcolor_arch_utils.position_encoding import PositionEmbeddingSine


class DDColor(nn.Module):
    def __init__(
            self,
            encoder_name='convnext-l',
            decoder_name='MultiScaleColorDecoder',
            num_input_channels=3,
            input_size=(256, 256),
            nf=512,
            num_output_channels=3,
            last_norm='Weight',
            do_normalize=False,
            num_queries=256,
            num_scales=3,
            dec_layers=9,
    ):
        super().__init__()

        self.encoder = ImageEncoder(encoder_name, ['norm0', 'norm1', 'norm2', 'norm3'])
        self.encoder.eval()
        test_input = torch.randn(1, num_input_channels, *input_size)
        self.encoder(test_input)

        self.decoder = DuelDecoder(
            self.encoder.hooks,
            nf=nf,
            last_norm=last_norm,
            num_queries=num_queries,
            num_scales=num_scales,
            dec_layers=dec_layers,
            decoder_name=decoder_name
        )

        self.refine_net = nn.Sequential(
            custom_conv_layer(num_queries + 3, num_output_channels, ks=1, use_activ=False, norm_type=NormType.Spectral)
        )

        self.do_normalize = do_normalize
        self.register_buffer('mean', torch.Tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer('std', torch.Tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def normalize(self, img):
        return (img - self.mean) / self.std

    def denormalize(self, img):
        return img * self.std + self.mean

    def forward(self, x):
        if x.shape[1] == 3:
            x = self.normalize(x)

        self.encoder(x)
        out_feat = self.decoder()
        coarse_input = torch.cat([out_feat, x], dim=1)
        out = self.refine_net(coarse_input)

        if self.do_normalize:
            out = self.denormalize(out)
        return out


class ImageEncoder(nn.Module):
    def __init__(self, encoder_name, hook_names):
        super().__init__()

        assert encoder_name == 'convnext-t' or encoder_name == 'convnext-l'
        if encoder_name == 'convnext-t':
            self.arch = ConvNeXt(depths=[3, 3, 9, 3], dims=[96, 192, 384, 768])
        elif encoder_name == 'convnext-l':
            self.arch = ConvNeXt(depths=[3, 3, 27, 3], dims=[192, 384, 768, 1536])
        else:
            raise NotImplementedError

        self.encoder_name = encoder_name
        self.hook_names = hook_names
        self.hooks = self.setup_hooks()

    def setup_hooks(self):
        hooks = [Hook(self.arch._modules[name]) for name in self.hook_names]
        return hooks

    def forward(self, x):
        return self.arch(x)


class DuelDecoder(nn.Module):
    def __init__(
            self,
            hooks,
            nf=512,
            blur=True,
            last_norm='Weight',
            num_queries=256,
            num_scales=3,
            dec_layers=9,
            decoder_name='MultiScaleColorDecoder',
    ):
        super().__init__()
        self.hooks = hooks
        self.nf = nf
        self.blur = blur
        self.last_norm = getattr(NormType, last_norm)
        self.decoder_name = decoder_name

        self.layers = self.make_layers()
        embed_dim = nf // 2
        self.last_shuf = CustomPixelShuffle_ICNR(embed_dim, embed_dim, blur=self.blur, norm_type=self.last_norm,
                                                 scale=4)

        assert decoder_name == 'MultiScaleColorDecoder'
        self.color_decoder = MultiScaleColorDecoder(
            in_channels=[512, 512, 256],
            num_queries=num_queries,
            num_scales=num_scales,
            dec_layers=dec_layers,
        )

    def make_layers(self):
        decoder_layers = []
        in_c = self.hooks[-1].feature.shape[1]
        out_c = self.nf

        setup_hooks = self.hooks[-2::-1]
        for layer_index, hook in enumerate(setup_hooks):
            feature_c = hook.feature.shape[1]
            if layer_index == len(setup_hooks) - 1:
                out_c = out_c // 2
            decoder_layers.append(
                UnetBlockWide(
                    in_c, feature_c, out_c, hook, blur=self.blur, self_attention=False, norm_type=NormType.Spectral))
            in_c = out_c

        return nn.Sequential(*decoder_layers)

    def forward(self):
        encode_feat = self.hooks[-1].feature
        out0 = self.layers[0](encode_feat)
        out1 = self.layers[1](out0)
        out2 = self.layers[2](out1)
        out3 = self.last_shuf(out2)

        return self.color_decoder([out0, out1, out2], out3)


class MultiScaleColorDecoder(nn.Module):
    def __init__(
            self,
            in_channels,
            hidden_dim=256,
            num_queries=100,
            nheads=8,
            dim_feedforward=2048,
            dec_layers=9,
            pre_norm=False,
            color_embed_dim=256,
            enforce_input_project=True,
            num_scales=3,
    ):
        super().__init__()

        self.hidden_dim = hidden_dim
        self.num_queries = num_queries
        self.num_layers = dec_layers
        self.num_feature_levels = num_scales

        # Positional encoding layer
        self.pe_layer = PositionEmbeddingSine(hidden_dim // 2, normalize=True)

        # Learnable query features and embeddings
        self.query_feat = nn.Embedding(num_queries, hidden_dim)
        self.query_embed = nn.Embedding(num_queries, hidden_dim)

        # Learnable level embeddings
        self.level_embed = nn.Embedding(num_scales, hidden_dim)

        # Input projection layers
        self.input_proj = nn.ModuleList(
            [self._make_input_proj(in_ch, hidden_dim, enforce_input_project) for in_ch in in_channels]
        )

        # Transformer layers
        self.transformer_self_attention_layers = nn.ModuleList()
        self.transformer_cross_attention_layers = nn.ModuleList()
        self.transformer_ffn_layers = nn.ModuleList()

        for _ in range(dec_layers):
            self.transformer_self_attention_layers.append(
                SelfAttentionLayer(
                    d_model=hidden_dim,
                    nhead=nheads,
                    dropout=0.0,
                    normalize_before=pre_norm,
                )
            )
            self.transformer_cross_attention_layers.append(
                CrossAttentionLayer(
                    d_model=hidden_dim,
                    nhead=nheads,
                    dropout=0.0,
                    normalize_before=pre_norm,
                )
            )
            self.transformer_ffn_layers.append(
                FFNLayer(
                    d_model=hidden_dim,
                    dim_feedforward=dim_feedforward,
                    dropout=0.0,
                    normalize_before=pre_norm,
                )
            )

        # Layer normalization for the decoder output
        self.decoder_norm = nn.LayerNorm(hidden_dim)

        # Output embedding layer
        self.color_embed = MLP(hidden_dim, hidden_dim, color_embed_dim, 3)

    def forward(self, x, img_features):
        assert len(x) == self.num_feature_levels

        src, pos = self._get_src_and_pos(x)

        bs = src[0].shape[1]

        # Prepare query embeddings (QxNxC)
        query_embed = self.query_embed.weight.unsqueeze(1).repeat(1, bs, 1)
        output = self.query_feat.weight.unsqueeze(1).repeat(1, bs, 1)

        for i in range(self.num_layers):
            level_index = i % self.num_feature_levels
            # attention: cross-attention first
            output = self.transformer_cross_attention_layers[i](
                output, src[level_index],
                memory_mask=None,
                memory_key_padding_mask=None,
                pos=pos[level_index], query_pos=query_embed
            )
            output = self.transformer_self_attention_layers[i](
                output, tgt_mask=None,
                tgt_key_padding_mask=None,
                query_pos=query_embed
            )
            # FFN
            output = self.transformer_ffn_layers[i](
                output
            )

        decoder_output = self.decoder_norm(output).transpose(0, 1)
        color_embed = self.color_embed(decoder_output)

        out = torch.einsum("bqc,bchw->bqhw", color_embed, img_features)

        return out

    def _make_input_proj(self, in_ch, hidden_dim, enforce):
        if in_ch != hidden_dim or enforce:
            proj = nn.Conv2d(in_ch, hidden_dim, kernel_size=1)
            nn.init.kaiming_uniform_(proj.weight, a=1)
            if proj.bias is not None:
                nn.init.constant_(proj.bias, 0)
            return proj
        return nn.Sequential()

    def _get_src_and_pos(self, x):
        src, pos = [], []
        for i, feature in enumerate(x):
            pos.append(self.pe_layer(feature).flatten(2).permute(2, 0, 1))  # flatten NxCxHxW to HWxNxC
            src.append(
                (self.input_proj[i](feature).flatten(2) + self.level_embed.weight[i][None, :, None]).permute(2, 0, 1))
        return src, pos


class ImageColorizationPipeline:
    def __init__(self, model_path, input_size=256, model_size='large'):
        self.input_size = input_size
        self.device = torch.device('cpu')

        self.encoder_name = 'convnext-t' if model_size == 'tiny' else 'convnext-l'
        self.decoder_type = 'MultiScaleColorDecoder'

        self.model = DDColor(
            encoder_name=self.encoder_name,
            decoder_name=self.decoder_type,
            input_size=[self.input_size, self.input_size],
            num_output_channels=2,
            last_norm='Spectral',
            do_normalize=False,
            num_queries=100,
            num_scales=3,
            dec_layers=9,
        ).to(self.device)

        # Load model weights
        self.model.load_state_dict(
            torch.load(model_path, map_location='cpu')['params'],
            #torch.load(model_path, map_location='cpu'),
            strict=True
        )
        self.model.eval()

    @torch.no_grad()
    def process(self, img):
        height, width = img.shape[:2]

        img = (img / 255.0).astype(np.float32)
        orig_l = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)[:, :, :1]  # (h, w, 1)

        # Resize and convert image to grayscale
        img_resized = cv2.resize(img, (self.input_size, self.input_size))
        img_l = cv2.cvtColor(img_resized, cv2.COLOR_BGR2Lab)[:, :, :1]
        img_gray_lab = np.concatenate((img_l, np.zeros_like(img_l), np.zeros_like(img_l)), axis=-1)
        img_gray_rgb = cv2.cvtColor(img_gray_lab, cv2.COLOR_LAB2RGB)

        tensor_gray_rgb = torch.from_numpy(img_gray_rgb.transpose((2, 0, 1))).float().unsqueeze(0).to(self.device)
        output_ab = self.model(tensor_gray_rgb).cpu()  # (1, 2, self.input_size, self.input_size)

        # Resize output and concatenate with original L channel
        output_ab_resized = F.interpolate(output_ab, size=(height, width))[0].float().numpy().transpose(1, 2, 0)
        output_lab = np.concatenate((orig_l, output_ab_resized), axis=-1)
        output_bgr = cv2.cvtColor(output_lab, cv2.COLOR_LAB2BGR)

        output_img = (output_bgr * 255.0).round().astype(np.uint8)
        return output_img



def main():
    print('此程序修改于阿里达摩院的DDColor项目，开源地址：https://github.com/piddnad/DDColor')
    print('此程序修改于阿里达摩院的DDColor项目，开源地址：https://github.com/piddnad/DDColor')
    print('此程序修改于阿里达摩院的DDColor项目，开源地址：https://github.com/piddnad/DDColor')
    cwd = os.getcwd()

    model_dir = os.path.join(cwd, 'model')
    input_dir = os.path.join(cwd, 'before')
    output_dir = os.path.join(cwd, 'after')
    input_size = 256
    model_size = 'large'

    print(f'输出目录：{output_dir}')
    os.makedirs(output_dir, exist_ok=True)

    model_files = [f for f in os.listdir(model_dir) if f.endswith('.pth')]
    assert len(model_files) > 0, "模型目录中未找到 .pth 文件"

    file_list = os.listdir(input_dir)
    assert len(file_list) > 0, "输入目录中未找到任何图片"

    for model_file in model_files:
        model_path = os.path.join(model_dir, model_file)
        print(f"\n使用模型：{model_file}")
        colorizer = ImageColorizationPipeline(model_path=model_path, input_size=input_size, model_size=model_size)

        for file_name in tqdm(file_list, desc=f"正在处理：{model_file}"):
            img_path = os.path.join(input_dir, file_name)
            output_name = os.path.splitext(file_name)[0] + f"_{os.path.splitext(model_file)[0]}_{input_size}.png"
            output_path = os.path.join(output_dir, output_name)

            if os.path.exists(output_path):
                print(f"跳过 {file_name}，输出文件已存在")
                continue

            img = cv2.imread(img_path)
            if img is not None:
                image_out = colorizer.process(img)
                cv2.imwrite(output_path, image_out)
            else:
                print(f"读取失败：{img_path}")

if __name__ == '__main__':
    main()
