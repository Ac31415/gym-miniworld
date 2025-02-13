import copy
import glob
import os
import time
import types
from collections import deque

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

# display results on tensorboard
import tensorboardX

import algo
from arguments import get_args
from envs import make_vec_envs
from model import Policy
from storage import RolloutStorage
#from visualize import visdom_plot

# import util func
import utils

# use datetime to make unique name
import time
import datetime

args = get_args()

assert args.algo in ['a2c', 'ppo', 'acktr']
if args.recurrent_policy:
    assert args.algo in ['a2c', 'ppo'], \
        'Recurrent policy is not implemented for ACKTR'

num_updates = int(args.num_frames) // args.num_steps // args.num_processes

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

try:
    os.makedirs(args.log_dir)
except OSError:
    files = glob.glob(os.path.join(args.log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)

eval_log_dir = args.log_dir + "_eval"

try:
    os.makedirs(eval_log_dir)
except OSError:
    files = glob.glob(os.path.join(eval_log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)


def main():
    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    """
    if args.vis:
        from visdom import Visdom
        viz = Visdom(port=args.port)
        win = None
    """

    date = datetime.datetime.now().strftime("%y-%m-%d-%H-%M-%S")

    # save_path_csv = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.num_steps) + " forward steps in A2C", str(args.lr) + " learning rate", args.env_name, date)
    #
    # csv_file, csv_logger = utils.get_csv_logger(save_path_csv)

    episode = 0

    envs = make_vec_envs(args.env_name, args.seed, args.num_processes,
                        args.gamma, args.log_dir, args.add_timestep, device, False)

    actor_critic = Policy(envs.observation_space.shape, envs.action_space,
        base_kwargs={'recurrent': args.recurrent_policy})
    actor_critic.to(device)


    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, lr=args.lr,
                               eps=args.eps, alpha=args.alpha,
                               max_grad_norm=args.max_grad_norm)
        save_path_csv = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.num_steps) + " forward steps in A2C", str(args.lr) + " learning rate", args.env_name, date)
        save_path = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.num_steps) + " forward steps in A2C", str(args.lr) + " learning rate", args.env_name, date)
    elif args.algo == 'ppo':
        agent = algo.PPO(actor_critic, args.clip_param, args.ppo_epoch, args.num_mini_batch,
                         args.value_loss_coef, args.entropy_coef, lr=args.lr,
                               eps=args.eps,
                               max_grad_norm=args.max_grad_norm)
        save_path_csv = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.ppo_epoch) + " epochs", str(args.num_mini_batch) + " batches", "clip parameter of " + str(args.clip_param), str(args.lr) + " learning rate", args.env_name, date)
        save_path = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.ppo_epoch) + " epochs", str(args.num_mini_batch) + " batches", "clip parameter of " + str(args.clip_param), str(args.lr) + " learning rate", args.env_name, date)
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, acktr=True)
        save_path_csv = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.lr) + " learning rate", args.env_name, date)
        save_path = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.lr) + " learning rate", args.env_name, date)

    csv_file, csv_logger = utils.get_csv_logger(save_path_csv)

    rollouts = RolloutStorage(args.num_steps, args.num_processes,
                        envs.observation_space.shape, envs.action_space,
                        actor_critic.recurrent_hidden_state_size)

    obs = envs.reset()
    rollouts.obs[0].copy_(obs)
    rollouts.to(device)

    episode_rewards = deque(maxlen=100)

    all_updates_rewards_np = []
    header = ["episode", "reward"]

    start = time.time()
    for j in range(num_updates):

        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value, action, action_log_prob, recurrent_hidden_states = actor_critic.act(
                        rollouts.obs[step],
                        rollouts.recurrent_hidden_states[step],
                        rollouts.masks[step])

            # Obser reward and next obs
            obs, reward, done, infos = envs.step(action)

            """
            for info in infos:
                if 'episode' in info.keys():
                    print(reward)
                    episode_rewards.append(info['episode']['r'])
            """

            # FIXME: works only for environments with sparse rewards
            for idx, eps_done in enumerate(done):
                if eps_done:
                    episode_rewards.append(reward[idx])

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            rollouts.insert(obs, recurrent_hidden_states, action, action_log_prob, value, reward, masks)

        with torch.no_grad():
            next_value = actor_critic.get_value(rollouts.obs[-1],
                                                rollouts.recurrent_hidden_states[-1],
                                                rollouts.masks[-1]).detach()

        rollouts.compute_returns(next_value, args.use_gae, args.gamma, args.tau)

        value_loss, action_loss, dist_entropy = agent.update(rollouts)

        rollouts.after_update()

        if j % args.save_interval == 0 and args.save_dir != "":
            print('Saving model')
            print()

            # save_path = os.path.join(args.save_dir, args.algo)
            # save_path = os.path.join(args.save_dir, args.algo, str(args.num_frames) + " frames", str(args.num_processes) + " CPU processes", str(args.num_steps) + " forward steps in A2C", str(args.lr) + " learning rate", args.env_name, date)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            # A really ugly way to save a model to CPU
            save_model = actor_critic
            if args.cuda:
                save_model = copy.deepcopy(actor_critic).cpu()

            save_model = [save_model, hasattr(envs.venv, 'ob_rms') and envs.venv.ob_rms or None]

            torch.save(save_model, os.path.join(save_path, args.env_name + ".pt"))

        total_num_steps = (j + 1) * args.num_processes * args.num_steps

        if j == 0:
            csv_logger.writerow(header)

        if len(episode_rewards) >= 1:
            all_updates_rewards_np = np.append(all_updates_rewards_np, np.mean(episode_rewards))

            # for j in i.numpy():
            #     data = [episode, j]

            data = [j, np.mean(episode_rewards)]
            csv_logger.writerow(data)
            csv_file.flush()
        else:
            all_updates_rewards_np = np.append(all_updates_rewards_np, 0)

            # for j in i.numpy():
            #     data = [episode, j]

            data = [j, 0]
            csv_logger.writerow(data)
            csv_file.flush()

        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            end = time.time()
            print("Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.2f}/{:.2f}, min/max reward {:.2f}/{:.2f}, success rate {:.2f}\n".
                format(
                    j, total_num_steps,
                    int(total_num_steps / (end - start)),
                    len(episode_rewards),
                    np.mean(episode_rewards),
                    np.median(episode_rewards),
                    np.min(episode_rewards),
                    np.max(episode_rewards),
                    np.count_nonzero(np.greater(episode_rewards, 0)) / len(episode_rewards)
                )
            )

            # torch.set_printoptions(threshold=10_000)

            # header = ["episode", "reward"]
            # data = [episode, num_frames, fps, duration]
            # csv_logger.writerow(data)
            # csv_file.flush()

            # episode_rewards_np = []
            #
            # header = ["episode", "reward"]
            #
            # if episode == 0:
            #     csv_logger.writerow(header)
            #
            # for i in episode_rewards:
            #     # episode_rewards_np = i.numpy()
            #     episode_rewards_np = np.append(episode_rewards_np, i.numpy())
            #     all_episode_rewards_np = np.append(all_episode_rewards_np, i.numpy())
            #
            #     # for j in i.numpy():
            #     #     data = [episode, j]
            #
            #     data = [episode, (i.numpy())[0]]
            #     csv_logger.writerow(data)
            #     csv_file.flush()
            #     episode += 1
            #     # print(episode_rewards_np)
            #
            # # episode_rewards_np = episode_rewards[0].numpy()
            #
            # # print(episode_rewards_np)
            # # print(all_episode_rewards_np)

        if args.eval_interval is not None and len(episode_rewards) > 1 and j % args.eval_interval == 0:
            eval_envs = make_vec_envs(args.env_name, args.seed + args.num_processes, args.num_processes,
                                args.gamma, eval_log_dir, args.add_timestep, device, True)

            if eval_envs.venv.__class__.__name__ == "VecNormalize":
                eval_envs.venv.ob_rms = envs.venv.ob_rms

                # An ugly hack to remove updates
                def _obfilt(self, obs):
                    if self.ob_rms:
                        obs = np.clip((obs - self.ob_rms.mean) / np.sqrt(self.ob_rms.var + self.epsilon), -self.clipob, self.clipob)
                        return obs
                    else:
                        return obs

                eval_envs.venv._obfilt = types.MethodType(_obfilt, envs.venv)

            eval_episode_rewards = []

            obs = eval_envs.reset()
            eval_recurrent_hidden_states = torch.zeros(args.num_processes,
                            actor_critic.recurrent_hidden_state_size, device=device)
            eval_masks = torch.zeros(args.num_processes, 1, device=device)

            while len(eval_episode_rewards) < 10:
                with torch.no_grad():
                    _, action, _, eval_recurrent_hidden_states = actor_critic.act(
                        obs, eval_recurrent_hidden_states, eval_masks, deterministic=True)

                # Obser reward and next obs
                obs, reward, done, infos = eval_envs.step(action)
                eval_masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
                for info in infos:
                    if 'episode' in info.keys():
                        eval_episode_rewards.append(info['episode']['r'])

            eval_envs.close()

            print(" Evaluation using {} episodes: mean reward {:.5f}\n".format(
                len(eval_episode_rewards),
                np.mean(eval_episode_rewards)
            ))

        """
        if args.vis and j % args.vis_interval == 0:
            try:
                # Sometimes monitor doesn't properly flush the outputs
                win = visdom_plot(viz, win, args.log_dir, args.env_name,
                                  args.algo, args.num_frames)
            except IOError:
                pass
        """

    envs.close()

if __name__ == "__main__":
    main()
