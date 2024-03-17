import logging
import numpy as np
import torch
from tqdm import tqdm
from utils.my_utils import save_model, load_model, EarlyStopMonitor, Metric
import time
from model.CTCP import CTCP
import math
from utils.data_processing import Data
from typing import Tuple, Dict, Type
from torch.nn.modules.loss import _Loss
from torch.distributions.normal import Normal
from utils.my_utils import compute_loss


def select_label(labels, types):  # 这个返回的应该是bool值，可以根据这个选出对于的位置
    train_idx = (labels != -1) & (types == 1)
    val_idx = (labels != -1) & (types == 2)
    test_idx = (labels != -1) & (types == 3)
    return {'train': train_idx, 'val': val_idx, 'test': test_idx}


def move_to_device(device, *args):
    results = []
    for arg in args:
        if type(arg) is torch.Tensor:
            results.append(arg.to(dtype=torch.float, device=device))
        else:
            results.append(torch.tensor(arg, device=device, dtype=torch.float))
    return results


# 重新又跑了一边，但是这一边不再更新网络的参数
def eval_model(model: CTCP, eval: Data, decoder_data, device: torch.device, param: Dict, metric: Metric,
               loss_criteria: _Loss, move_final: bool = False) -> Dict:
    model.eval()
    model.reset_state()
    metric.fresh()
    epoch_metric = {}
    loss = {'train': [], 'val': [], 'test': []}
    z0_prior = Normal(torch.Tensor([0.0]).to(device), torch.Tensor([1.]).to(device))
    with torch.no_grad():
        for x, label in tqdm(eval.loader(param['bs']), total=math.ceil(eval.length / param['bs']), desc='eval_or_test'):
            src, dst, trans_cas, trans_time, pub_time, types = x
            index_dict = select_label(label, types)
            target_idx = index_dict['train'] | index_dict['val'] | index_dict['test']
            trans_time, pub_time, label = move_to_device(device, trans_time, pub_time, label)
            pred, extra_info = model.forward(src, dst, trans_cas, trans_time, pub_time, target_idx)
            first_point = extra_info['first_point']
            for dtype in ['train', 'val', 'test']:
                idx = index_dict[dtype]
                if sum(idx) > 0:
                    m_target = trans_cas[idx]
                    m_label = torch.tensor([decoder_data[key] for key in m_target])
                    m_label[m_label < 1] = 1
                    # m_label = torch.log2(m_label)
                    m_pred = pred[idx]
                    m_first_point = first_point[idx]
                    loss[dtype].append(
                        compute_loss(pred=m_pred, label=m_label, first_point=m_first_point, z0_prior=z0_prior).item())
                    metric.update(target=m_target, pred=m_pred.cpu().numpy(), label=m_label.cpu().numpy(), dtype=dtype)
            model.update_state()
        for dtype in ['train', 'val', 'test']:
            epoch_metric[dtype] = metric.calculate_metric(dtype, move_history=True, move_final=move_final,
                                                          loss=np.mean(loss[dtype]))
        return epoch_metric


def train_model(num: int, dataset: Data, decoder_data, model: CTCP, logger: logging.Logger,
                early_stopper: EarlyStopMonitor,
                device: torch.device, param: Dict, metric: Metric, result: Dict):
    train, val, test = dataset, dataset, dataset
    model = model.to(device)
    logger.info('Start training citation')
    optimizer = torch.optim.Adam(model.parameters(), lr=param['lr'])
    z0_prior = Normal(torch.Tensor([0.0]).to(device), torch.Tensor([1.]).to(device))
    loss_criterion = torch.nn.MSELoss()
    for epoch in range(param['epoch']):
        model.reset_state()
        model.train()
        logger.info(f'Epoch {epoch}:')
        epoch_start = time.time()
        train_loss = []
        for x, label in tqdm(train.loader(param['bs']), total=math.ceil(train.length / param['bs']),
                             desc='training'):
            src, dst, trans_cas, trans_time, pub_time, types = x  # 数据处理之后得到的吗，可以自动完成，tran_cas是级联id
            idx_dict = select_label(label, types)  # 训练集，与id的一个字典，label不等于-1代表到达了观测时间
            target_idx = idx_dict['train']  # 训练集的id
            trans_time, pub_time, label = move_to_device(device, trans_time, pub_time, label)
            pred, extra_info = model.forward(src, dst, trans_cas, trans_time, pub_time, target_idx)
            if sum(target_idx) > 0:
                target, target_time = trans_cas[target_idx], trans_time[target_idx]
                target_label = torch.tensor([decoder_data[key] for key in target])
                target_label[target_label < 1] = 1
                # target_label = torch.log2(target_label)  # 对数化
                target_pred = pred[target_idx]
                first_point = extra_info['first_point']
                optimizer.zero_grad()
                # loss = loss_criterion(target_pred, target_label)  # loss是针对已经预测的来做的
                loss = compute_loss(target_pred, target_label, first_point=first_point, z0_prior=z0_prior)
                loss.backward()
                optimizer.step()
                train_loss.append(loss.item())
            model.update_state()
            model.detach_state()
        epoch_end = time.time()
        epoch_metric = eval_model(model, val, decoder_data, device, param, metric, loss_criterion, move_final=False)
        logger.info(f"Epoch{epoch}: time_cost:{epoch_end - epoch_start} train_loss:{np.mean(train_loss)}")
        for dtype in ['train', 'val', 'test']:
            metric.info(dtype)
        if early_stopper.early_stop_check(epoch_metric['val']['msle']):
            break
        else:
            ...
    logger.info('No improvement over {} epochs, stop training'.format(early_stopper.max_round))
    logger.info(f'Loading the best model at epoch {early_stopper.best_epoch}')
    load_model(model, param['model_path'], num)
    logger.info(f'Loaded the best model at epoch {early_stopper.best_epoch} for inference')
    final_metric = eval_model(model, test, device, param, metric, loss_criterion, move_final=True)
    logger.info(f'Runs:{num}\n {metric.history}')
    metric.save()
    save_model(model, param['model_path'], num)

    result['msle'] = np.round(result['msle'] + final_metric['test']['msle'] / param['run'], 4)
    result['mape'] = np.round(result['mape'] + final_metric['test']['mape'] / param['run'], 4)
    result['male'] = np.round(result['male'] + final_metric['test']['male'] / param['run'], 4)
    result['pcc'] = np.round(result['pcc'] + final_metric['test']['pcc'] / param['run'], 4)
