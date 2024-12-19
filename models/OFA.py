import torch
import torch.nn as nn

from transformers.models.gpt2.modeling_gpt2 import GPT2Model , GPT2LMHeadModel 
from einops import rearrange
from transformers.models.gpt2.configuration_gpt2 import GPT2Config
from  layers.SelfAttention_Family import MultiHeadAttention
    
class LinearLayerOnSecondDim(nn.Module):
    def __init__(self, input_dim, output_dim):
        super(LinearLayerOnSecondDim, self).__init__()
        self.linear = nn.Linear(input_dim, output_dim)
        
    def forward(self, x):
        # [batch_size, 42, 768]
        batch_size, seq_len, embed_dim = x.shape
        # [batch_size, 42, 768] -> [batch_size*42, 768]
        x = x.view(batch_size, -1)
        x = self.linear(x)
        x = x.view(batch_size, seq_len, -1)
        return x

class Encoder_TRSF(nn.Module):
    def __init__(self, input_dim=0 , hidden_dim=768, num_heads=12, num_encoder_layers=1):
        super(Encoder_TRSF, self).__init__()
        encoder_layer = nn.TransformerEncoderLayer(d_model=hidden_dim, nhead=num_heads)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_encoder_layers)
        
    def forward(self, x):
        x = self.transformer_encoder(x.transpose(0, 1)).transpose(0, 1)
        return x 

class Model(nn.Module):
    """
    One Fits All: Power General Time Series Analysis by Pretrained LM (NeurIPS 2023 Spotlight)

    arxiv: https://arxiv.org/abs/2302.11939
    
    github: https://github.com/DAMO-DI-ML/NeurIPS2023-One-Fits-All
    
    citation: @inproceedings{zhou2023onefitsall,
        title={{One Fits All}: Power General Time Series Analysis by Pretrained LM},
        author={Tian Zhou, Peisong Niu, Xue Wang, Liang Sun, Rong Jin},
        booktitle={NeurIPS},
        year={2023}
    }
    """    
    
    def __init__(self, configs, device , log_fine_name = None ):
        super(Model, self).__init__()
        
        self.is_gpt = configs.is_gpt
        self.patch_size = configs.patch_size
        self.pretrain = configs.pretrain
        self.stride = configs.stride
        self.patch_num = (configs.seq_len - self.patch_size) // self.stride + 1
        self.gpt_layers = configs.gpt_layers 
        self.gpt_layers = 6
        self.padding_patch_layer = nn.ReplicationPad1d((0, self.stride)) 
        self.patch_num += 1
        self.ts_scale = -100
        self.log_fine_name = log_fine_name 
        self.n_scale = configs.n_scale
        
        if configs.is_gpt:
            if configs.pretrain:
                self.gpt2 = GPT2Model.from_pretrained(
                    'gpt2', attn_implementation="eager", 
                    output_hidden_states=True
                )  # loads a pretrained GPT-2 base model
            else:
                print("------------------no pretrain------------------")
                self.gpt2 = GPT2Model(GPT2Config())
            self.gpt2.h = self.gpt2.h[:self.gpt_layers]
            # print("gpt2 = {}".format(self.gpt2))
        else : 
            self.gpt2 = GPT2LMHeadModel.from_pretrained("gpt2") 
            
        self.in_layer = nn.Linear(configs.patch_size, configs.d_model)
        self.out_layer = nn.Linear(configs.d_model * self.patch_num, configs.test_pred_len)
        
        # noised_wpe_param_ = None 
        if configs.freeze and configs.pretrain:
            for i, (name, param) in enumerate(self.gpt2.named_parameters()):
                # double-change adding noise 
                # if 'wpe' in name and self.n_scale > 0 : 
                #     print('perturbating WPE  ... ')
                #     noised_wpe_param_ = self.perturbate( param , log_fine_name )
                if 'ln' in name or 'wpe' in name:
                    param.requires_grad = True
                else:
                    param.requires_grad = False
                    
        for layer in (self.gpt2, self.in_layer, self.out_layer ):
            layer.to(device=device)
            layer.train()
            
        self.cnt = 0
            
    def forward(self, x, x_mark, y_mark):
        # x = rearrange(x, 'b l m -> b m l')
        B, L, M = x.shape
        # Normalization --twice
        means = x.mean(1, keepdim=True).detach()
        x = x - means
        stdev = torch.sqrt(torch.var(x, dim=1, keepdim=True, unbiased=False)+ 1e-5).detach() 
        x /= stdev
        
        x = rearrange(x, 'b l m -> b m l')
        # print(x.shape)
        
        # print('x1' , x.shape)  [256, 1, 336] 
        x = self.padding_patch_layer(x)
        # print('x2' ,x.shape) [256, 1, 344] 

        # patch_size 16 ; stride 8 
        x = x.unfold(dimension=-1, size=self.patch_size, step=self.stride)
        # print('x3' ,x.shape) [256, 1, 42, 16]
        
        x = rearrange(x, 'b m n p -> (b m) n p')
        # print('x4' ,x.shape)  [256,  42, 16]
        
        # Embeedding layer d_model = 768 
        outputs = self.in_layer(x)
        # print(outputs.shape)
        
        outputs = self.gpt2(inputs_embeds=outputs).last_hidden_state
        
        outputs = self.out_layer(outputs.reshape(B*M, -1))
        
        # print('outputs3' ,outputs.shape) [256, 96]
        outputs = rearrange(outputs, '(b m) l -> b l m', b=B)
        
        outputs = outputs * stdev
        outputs = outputs + means
        return outputs

    def perturbate(self , param , log_fine_name ):
        
        if 'zero' in log_fine_name : 
            param = torch.zeros_like(param).to(device=param.device, dtype=param.dtype)
            with open(log_fine_name , 'a') as f : 
                f.write('Param scale from 0.03 to 0 \n')
            return param
            
        n_scale = self.n_scale
        
        n_scale = float(n_scale)
        o_scale = torch.mean(torch.abs(param.clone())) 

        # Add noise here 
        noise = torch.randn_like(param).to(device=param.device, dtype=param.dtype) * n_scale
        param = param + noise
        
        no_scale = torch.mean(torch.abs(param.clone())) 
        with open(log_fine_name , 'a') as f : 
            f.write('n_scale:{} Param scale from {} to {} \n'.format(n_scale, o_scale , no_scale))

        return param    

    def inject_noise(self , noised_wpe_param_ , model , fix_wpe = True ):
        noised_wpe_param_ = noised_wpe_param_.to(model.device)
        model.wpe.weight.data = noised_wpe_param_.data
        for _, (name, param) in enumerate(model.named_parameters()):
            if 'wpe' in name:  
                print(name, param)
                if fix_wpe:
                    param.requires_grad = False
                    print('noised wpe has been frezen!!!!')

'''
# if self.ts_scale > -1 : 
        #     noise = torch.randn_like(x).to(device=x.device, dtype=x.dtype) * self.ts_scale 
        #     # print(torch.mean(torch.abs(x)))  20 0.7949
        #     x += noise
            # print(torch.mean(torch.abs(x)))  20 15.9119
            
            
            # # 'disturb_TS_0.txt'
        # if 'disturb_TS' in log_fine_name:
        #     n_scale = log_fine_name.split('_')[-1].split('.')[0]
        #     if 'p' in n_scale :
        #         n_scale = n_scale.replace('p' , '.')
        #     self.ts_scale = float(n_scale)

'''