import tensorflow as tf
import numpy as np      			
import os
import sys 
import json
import time
from datetime import datetime

from model import *
from rollout import Rollout
from env.ai2thor_env import AI2ThorDumpEnv

class MultitaskPolicy(object):

	def __init__(
			self,
			training_scene,
			training_object,
			config,
			arguments
			):

		self.config = config
		self.arguments = arguments

		self.use_gae = arguments.get('use_gae')
		self.num_epochs = arguments.get('num_epochs')
		self.num_episodes = arguments.get('num_episodes')
		self.num_iters = arguments.get('num_iters')
		self.gamma = arguments.get('gamma')
		self.lamb = arguments.get('lamb')
		self.lr = arguments.get('lr')
		self.joint_loss = arguments.get('joint_loss')
		self.ec = arguments.get('ec')
		self.vc = arguments.get('vc')
		self.max_grad_norm = arguments.get('max_gradient_norm')
		self.decay = arguments.get('decay')
		self.reuse = arguments.get('share_latent')

		self.env = AI2ThorDumpEnv(training_scene, training_object, config, arguments)
		self.rollout = Rollout(training_scene, training_object, config, arguments)

		tf.reset_default_graph()

		self.PGNetwork  = A2C(name='A2C', 
							state_size=self.env.features.shape[1], 
							action_size=self.env.action_space,
							history_size=arguments['history_size'],
							entropy_coeff=self.ec,
							value_function_coeff=self.vc,
							max_gradient_norm=self.max_grad_norm,
							joint_loss=self.joint_loss,
							learning_rate=self.lr,
							decay=self.decay,
							reuse=bool(self.reuse)
							)

		if self.decay:
			self.PGNetwork.set_lr_decay(self.lr, self.num_epochs * self.num_episodes * self.num_iters)
			
		print("\nInitialized network with {} trainable weights.".format(len(self.PGNetwork.find_trainable_variables('A2C', True))))

		gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction = 0.15)

		self.sess = tf.Session(config = tf.ConfigProto(gpu_options = gpu_options))
		self.sess.run(tf.global_variables_initializer())

		self.saver = tf.train.Saver()
		
		timer = "{}_{}_{}".format(str(datetime.now()).replace(" ", "-").replace(".", "-").replace(":", "-"), training_scene, training_object)
		self.log_folder = os.path.join(arguments.get('logging'), timer)
		self.writer = tf.summary.FileWriter(self.log_folder)
		
		test_name =  training_scene
		tf.summary.scalar(test_name + "/" + training_object, self.PGNetwork.mean_reward)

		self.write_op = tf.summary.merge_all()

	def discount_with_dones(self, rewards, dones, gamma):
		discounted = []
		r = 0
		# Start from downwards to upwards like Bellman backup operation.
		for reward, done in zip(rewards[::-1], dones[::-1]):
			r = reward + gamma * r * (1. - done)  # fixed off by one bug
			discounted.append(r)
		return discounted[::-1]

	def generalized_advantage_estimate(self, rewards, dones, values, last_value, gamma, lamb):
		advantages = np.zeros_like(rewards)
		lastgaelam = 0

        # From last step to first step
		for t in reversed(range(len(rewards))):
            # If t == before last step
			if t == len(rewards) - 1:
				# If a state is done, nextnonterminal = 0
				# In fact nextnonterminal allows us to do that logic

				#if done (so nextnonterminal = 0):
				#    delta = R - V(s) (because self.gamma * nextvalues * nextnonterminal = 0) 
				# else (not done)
				    #delta = R + gamma * V(st+1)
				nextnonterminal = 1.0 - dones[-1]

				# V(t+1)
				nextvalue = last_value
			else:
				nextnonterminal = 1.0 - dones[t]

				nextvalue = values[t+1]

			# Delta = R(t) + gamma * V(t+1) * nextnonterminal  - V(t)
			delta = rewards[t] + gamma * nextvalue * nextnonterminal - values[t]

			# Advantage = delta + gamma *  (lambda) * nextnonterminal  * lastgaelam
			advantages[t] = lastgaelam = delta + gamma * lamb * nextnonterminal * lastgaelam

		# advantages = (advantages - np.mean(advantages)) / (np.std(advantages) + 1e-6)
		return list(advantages)

	def _make_batch(self, sess):
		'''
		states = [
		    [---episode_1---],...,[---episode_n---]
		]
		same as actions, tasks, rewards, values, dones

		last_values = [
			episode_1, ...., episode_n]
		]
		'''
		states, task_logits, actions, rewards, values, last_values = self.rollout.rollout_batch(sess, self.PGNetwork)

		observations = []
		converted_actions = []
		logits = []

		for ep_idx, ep_states in enumerate(states):
			observations += [s.tolist() for s in ep_states]
			converted_actions += [self.env.cv_action_onehot[a] for a in actions[ep_idx]]
			logits += task_logits[ep_idx]

		returns = []
		advantages = []

		for ep_idx, (ep_rewards, ep_states) in enumerate(zip(rewards, states)):
			assert len(ep_rewards) == len(ep_states)
			ep_dones = list(np.zeros_like(ep_rewards))

			if ep_rewards[-1] != self.config['success_reward']:
				last_value = last_values[ep_idx]
				assert last_value is not None
				ep_returns = self.discount_with_dones(ep_rewards + [last_value], ep_dones+[0], self.gamma)[:-1]
			else:
				last_value = 0
				ep_dones[-1] = 1
				ep_returns = self.discount_with_dones(ep_rewards, ep_dones, self.gamma)

			returns += ep_returns
			ep_values = values[ep_idx]

			if not self.use_gae:
				# Here we calculate advantage A(s,a) = R + yV(s') - V(s)
		    	# rewards = R + yV(s')
				advantages += list((np.array(ep_returns) - np.array(ep_values)).astype(np.float32))

			else:
				advantages += self.generalized_advantage_estimate(ep_rewards, ep_dones, ep_values, last_value, self.gamma, self.lamb)
		
		return observations,\
				 converted_actions,\
				 returns,\
				 advantages,\
				 logits,\
				 rewards				 		
		
	def train(self):
		total_samples = 0

		start = time.time()
		for epoch in range(self.num_epochs):
			# sys.stdout.flush()
			
			# ROLLOUT SAMPLE
			#---------------------------------------------------------------------------------------------------------------------#	
			mb_states,\
			mb_actions,\
			mb_returns,\
			mb_advantages,\
			mb_logits,\
			rewards = self._make_batch(self.sess)
			
			#---------------------------------------------------------------------------------------------------------------------#	
			print('Time elapsed: {:.3f}, epoch {}/{}'.format((time.time() - start)/3600, epoch + 1, self.num_epochs), end = '\r', flush = True)

			sum_dict = {}
			assert len(mb_states) == len(mb_actions) == len(mb_returns) == len(mb_advantages)
			
			try:
				policy_loss, value_loss, _, _ = self.PGNetwork.learn(self.sess, actor_states = mb_states,
																	advantages = mb_advantages, actions = mb_actions,
																	critic_states = mb_states, returns = mb_returns,
																	task_logits = mb_logits)

			except:
				print(np.asarray(mb_states).shape)
				print(np.asarray(mb_actions).shape)
				print(np.asarray(mb_returns).shape)
				print(np.asarray(mb_advantages).shape)
				print(np.asarray(mb_logits).shape)
				

			sum_dict[self.PGNetwork.mean_reward] = \
						np.sum(np.concatenate(rewards)) / len(rewards)

			total_samples += len(list(np.concatenate(rewards)))

			#---------------------------------------------------------------------------------------------------------------------#	
			

			# WRITE TF SUMMARIES
			#---------------------------------------------------------------------------------------------------------------------#	
			summary = self.sess.run(self.write_op, feed_dict = sum_dict)

			self.writer.add_summary(summary, total_samples)
			self.writer.flush()
			#---------------------------------------------------------------------------------------------------------------------#	

		self.saver.save(self.sess, self.log_folder + "/my-model")
		self.sess.close()
		# SAVE MODEL
		#---------------------------------------------------------------------------------------------------------------------#	
		with open(self.log_folder + '/arguments.json', 'w') as outfile:
		    json.dump(self.arguments, outfile)

		print("\nElapsed time: {}".format((time.time() - start)/3600))	
		#---------------------------------------------------------------------------------------------------------------------#		