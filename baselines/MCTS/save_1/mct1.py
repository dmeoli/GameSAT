import os, time, pickle
import numpy as np
import scipy.sparse as sp
from minisat.minisat.gym.GymSolver import sat

def softmax(x):
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum()

def get_PI(counts, tau):
    p = 1 / tau
    counts = counts / counts.sum()
    # do it in small steps to prevent underflow
    while p >= 10:
        counts = np.power(counts, 10)
        p = p / 10
        counts = counts / counts.sum()
    Pi = np.power(counts, p)
    Pi = Pi / np.sum(Pi)
    return Pi

def get_nrepeat_count(action, nact):
    # given a numpy array of action, return an numpy array of the size of nact, and contains the counts of each element
    action = np.sort(action)
    counts = np.zeros(nact, dtype = int)
    base = 0
    start = 0
    for i in range(action.shape[0]):
        if action[i] > base:
            counts[base] = i - start
            start = i
            base = action[i]
    counts[base] = action.shape[0] - start
    return counts

def analyze_Pi_graph_dump(file_no, Pi_node, sl_Buffer, standard):
    if not Pi_node.evaluated: return # this is the finished node (no state, not evaluated)
    for act in Pi_node.children:
        analyze_Pi_graph_dump(file_no, Pi_node.children[act], sl_Buffer, standard)
    # save this node's infor TODO: should the score be more quatified?
    av = Pi_node.score / Pi_node.repeat 
    # for mct2.py, change the score calculation to tanh
    score = np.tanh((standard[1] - av) * 3.0 / standard[1])
    # if av > standard[1]: score = - (av-standard[1]) / (standard[2]-standard[1])
    # elif av < standard[1]: score = (standard[1]-av) / (standard[1]-standard[0])
    # else: score = 0
    sl_Buffer.add(file_no, Pi_node.state, Pi_node.Pi, score, Pi_node.repeat)

class Pi_struct:
    """
        inner class used by MCT class. It forms a tree structure and cache states, Pi, and other values for self play
    """
    def __init__(self, size, repeat, level, file_no, tau, parent = None):
        """
            size:    the size of Pi, which is the same as nact 
            repeat:  the number of repeats to play this file
            level:   the level is the number of steps to reach this node
            file_no: the index of this file in the file list (this is only used in error message)
            tau:     the function that returns a proper tau value given the current level
            parent:  the parent node of this Pi_struct
        """
        self.size = size
        self.repeat = repeat
        self.level = level
        self.file_no = file_no
        self.tau = tau
        self.parent = parent
        self.children = {}
        self.next = 0
        self.score = 0
        self.evaluated = False
        
    def add_state(self, state):
        """
            take the state of this Pi_struct, save it as sparse matrix, and also compute the isValid array
        """
        self.isValid = np.reshape(np.any(state, axis = 0), [self.size,])
        state_2d = np.reshape(state, [-1, state.shape[1] * state.shape[2]])
        self.state = sp.csc_matrix(state_2d)
    
    def add_counts(self, counts):
        """
            take the counts (of MCTS simulation from this state), generate Pi/nrepeats for children, and initialize the children
        """
        assert counts.sum() == (counts * self.isValid).sum(), "count: " + str(counts) + \
        " is invalid: " + str(self.isValid) + " in file " + str(self.file_no)
        # tau is a function that takes the current level as parameter and returns the proper tau value for this level
        self.Pi = get_PI(counts, self.tau(self.level)) 
        
        assert (self.isValid * self.Pi).sum() > 0.999999, "Pi: " + str(self.Pi) + \
        " is invalid: " + str(self.isValid) + " in file " + str(self.file_no)
        # random select actions based on Pi
        action = np.random.choice(range(self.size), self.repeat, p = self.Pi) 
        self.nrepeats = get_nrepeat_count(action, self.size)
        
        assert self.repeat == (self.nrepeats * self.isValid).sum(), "nrepeats: " + str(self.nrepeats) + \
        " is invalid: " + str(self.isValid) + " in file " + str(self.file_no)
        # create children of this Pi_struct
        for i in range(self.size):
            if self.nrepeats[i] > 0.5:
                self.children[i] = Pi_struct(self.size, self.nrepeats[i], self.level + 1, self.file_no, self.tau, parent = self)
        self.evaluated = True

    def get_next(self):
        """
            return the index of the next children to explore (this function should never reach -1 from MCT object, if set_next() is used properly)
        """
        while self.next < self.size and (self.nrepeats[self.next] < 0.5 or not self.isValid[self.next]):
            self.next += 1
        if self.next >= self.size: 
            return -1 # no more actions to go
        return self.next

    def set_next(self, additional_score):
        """
            this function progress this Pi_struct by adding the scores for the current exploration index, 
            find the next exploration index (increment and call get_next()), and recurse to the parent Pi_struct if DONE
            It returns a non-negative index if this Pi_struct or a parental Pi_struct still has exploration index, otherwise -1 (the get_next() value)
        """
        self.score += additional_score
        self.next += 1
        next = self.get_next()
        if (self.get_next() < 0 and self.parent is not None):
            return self.parent.set_next(self.score)
        return next

class MCT:
    def __init__(self, file_path, file_no, max_clause1, max_var1, nrepeat, tau, resign = 1000000):
        """
            file_path:   the directory to files that are used for training
            file_no:     the file index that this object works on (each MCT only focus on one file problem)
            max_clause1: the max_clause that should be passed to the env object
            max_var1:    the max_var that should be passed to the env object
            nrepeat:     the number of repeats that we want to self_play with this file problem (suggest 100)
            tau:         the function that, given the current number of step, return a proper tau value
        """
        self.env = sat(file_path, max_clause = max_clause1, max_var = max_var1) 
        self.file_no = file_no
        self.state = self.env.resetAt(file_no) 
        # IMPORTANT: all reset call should use the resetAt(file_no) function to make sure that it resets at the same file
        if self.state is None: # extreme case where the SAT problem is solved by simplification
            self.Pi_root = None
            self.phase = None
        else: # normal case: set up!
            self.Pi_current = self.Pi_root = Pi_struct(max_var1 * 2, nrepeat, 0, file_no, tau) 
            self.Pi_current.add_state(self.state)
            self.min_step = resign
            self.max_step = 0
            self.resign = resign # if the number of steps is larger than resign, treat as lost and done
            self.phase = False 
            # phase False is "initial and normal running" phase, 
            # phase True is "pause and return state" phase
            # pahse None is "the problem is finished" phase

    def get_state(self, pi_array, v_value):
        """
            main logic function:
            pi_array: the pi array evaluated by neural net (when phase is False, this paramete is not used)
            v_value:  the v value evaluated by neural net  (when phase is False, this paramete is not used)
            Return a state (3d numpy array) if paused for evaluation.
            Return None if this problem is simulated nrepeat times (all required repeat times are finished)
        """
        if self.phase is None: 
            return None
        while True:
            if not self.Pi_current.evaluated:
                needEnv = True
                while needEnv or needSim:
                    if needEnv:
                        if not self.phase:
                            self.phase = True
                            return self.state
                        else:
                            self.phase = False
                    self.state, needEnv, needSim = self.env.simulate(softmax(pi_array), v_value)
                self.Pi_current.add_counts(self.env.get_visit_count())

            next_act = self.Pi_current.get_next() 
            assert next_act >= 0, "next_act is neg in file " + str(self.file_no)
            isDone, self.state = self.env.step(next_act) # there is a guarantee that this next_act is valid
            if isDone or self.Pi_current.level >= self.resign:
                if self.Pi_current.level < self.min_step: self.min_step = self.Pi_current.level # update score range for this sat prob
                if self.Pi_current.level > self.max_step: self.max_step = self.Pi_current.level
                if (self.Pi_current.set_next(self.Pi_current.level * self.Pi_current.nrepeats[next_act]) < 0): # write back the total score from this leaf node
                    self.phase = None # mark self.phase as None to indicate that the object is finished
                    return None # we are finished
                self.Pi_current = self.Pi_root
                self.state = self.env.resetAt(self.file_no)
            else:
                self.Pi_current = self.Pi_current.children[next_act]
                self.Pi_current.add_state(self.state)

    def write_data_to_buffer(self, sl_Buffer):
        if self.Pi_root is None: return # there is nothing to write
        standard = (self.min_step, self.Pi_root.score / self.Pi_root.repeat, self.max_step)
        analyze_Pi_graph_dump(self.file_no, self.Pi_root, sl_Buffer, standard)

    def report_performance(self):
        if self.Pi_root is None:
            return self.file_no, 1, 1
        return self.file_no, self.Pi_root.repeat, self.Pi_root.score
