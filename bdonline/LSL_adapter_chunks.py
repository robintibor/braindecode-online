"""
Receive EEG-data from NeurOne and labels from the Unity game both via lsl.
Pass EEG-data and labels to braindecode-online via TCP/IP.
Simultaniously receive predictions from braindecode-online and publish them on lsl.
"""


import sys
import logging
import time
import functools
import numpy as np
from numpy.random import RandomState
from scipy import interpolate
from gevent import socket
import gevent.server
import gevent.select
from socket import timeout as socket_timeout
import signal
import pylsl as lsl
from scipy.signal import filtfilt, iirnotch, butter
from scipy.stats import ttest_ind


# log = logging.getLogger(__name__)
print = functools.partial(print, flush=True)



lsl_inlet_eeg = None                # lsl.StreamInlet to receive eeg from NeurOne
lsl_inlet_labels = None             # lsl.StreamInlet to receive labels from the Unity game
lsl_outlet_predictions = None       # lsl.StreamOutlet to publish predictions
tcp_recv_server_preds = None        # TCP/IP server to receive predictions from braindecode-online
tcp_recv_socket_preds = None        # TCP/IP socket to receive predictions from braindecode-online
tcp_send_socket_eeg = None          # TCP/IP socket to send eeg data to braindecode-online
tcp_recv_preds_connected = False    # indicates if the tcp receiver for predictions is connected


DEBUG = False                                       # DEBUG=True activates extra debug messages

TCP_SENDER_EEG_PORT = 7987                          # port of braindecode-online
TCP_SENDER_EEG_HOSTNAME = '127.0.0.1'               # hostname of braindecode-online
TCP_SENDER_EEG_NCHANS_TIME= 66                          # number of channels to send to braindecode-online, includes labels and timestamps
TCP_SENDER_EEG_NCHANS= 65                       # number of channels to send to braindecode-online, includes labels

TCP_SENDER_EEG_NSAMPLES = 500# number of samples * DOWNSAMPLING_COEF per channel to send to braindecode-online at once

LSL_RECEIVER_EEG_NAME = 'NeuroneStream'             # name of the lsl stream to receive EEG data

LSL_RECEIVER_LABELS_NAME = 'Game State'             # name of the lsl stram to receive labels from unity game

LSL_SENDER_PREDS_NAME = 'bdonline'                  # name of the lsl stream to publish predictions
LSL_SENDER_PREDS_TYPE = 'EEG'                       # type of the lsl stream to publish predictions
LSL_SENDER_PREDS_STREAMCHANNELS = 3                 # channels of the lsl stream to publish preds.
LSL_SENDER_PREDS_ID = 'braindecode preds'           # id of the lsl stream to publish preds.

TCP_RECEIVER_PREDS_HOSTNAME = 'localhost'           # hostname of this PC, used to receive predictions
TCP_RECEIVER_PREDS_PORT = 30000                     # port on this PC, used to receive predictions

EEG_CHANNELNAMES_TIME = ['Fp1', 'Fpz', 'Fp2', 'AF7',     # channel names send to braindecode-online
                  'AF3', 'AF4', 'AF8', 'F7',
                  'F5', 'F3', 'F1', 'Fz', 'F2', 'F4', 'F6', 'F8', 'FT7', 'FC5',
                  'FC3',
                  'FC1', 'FCz', 'FC2', 'FC4', 'FC6', 'FT8', 'M1', 'T7', 'C5',
                  'C3',
                  'C1', 'Cz', 'C2', 'C4', 'C6', 'T8', 'M2', 'TP7', 'CP5', 'CP3',
                  'CP1', 'CPz', 'CP2', 'CP4', 'CP6', 'TP8', 'P7', 'P5', 'P3',
                  'P1',
                  'Pz', 'P2', 'P4', 'P6', 'P8', 'PO7', 'PO5', 'PO3', 'POz',
                  'PO4',
                  'PO6', 'PO8', 'O1', 'Oz', 'O2', 'marker', 'time_stamp']
EEG_CHANNELNAMES = ['Fp1', 'Fpz', 'Fp2', 'AF7',     # channel names send to braindecode-online
                  'AF3', 'AF4', 'AF8', 'F7',
                  'F5', 'F3', 'F1', 'Fz', 'F2', 'F4', 'F6', 'F8', 'FT7', 'FC5',
                  'FC3',
                  'FC1', 'FCz', 'FC2', 'FC4', 'FC6', 'FT8', 'M1', 'T7', 'C5',
                  'C3',
                  'C1', 'Cz', 'C2', 'C4', 'C6', 'T8', 'M2', 'TP7', 'CP5', 'CP3',
                  'CP1', 'CPz', 'CP2', 'CP4', 'CP6', 'TP8', 'P7', 'P5', 'P3',
                  'P1',
                  'Pz', 'P2', 'P4', 'P6', 'P8', 'PO7', 'PO5', 'PO3', 'POz',
                  'PO4',
                  'PO6', 'PO8', 'O1', 'Oz', 'O2', 'marker']
PREDICTION_NUM_CLASSES = 2
TARGET_FS = 250
DOWNSAMPLING_COEF = int(5000 / TARGET_FS)
# Butter filter (highpass) for 1 Hz
B_1, A_1 = butter(6, 1 / TARGET_FS, btype='high', output='ba')

# Butter filter (lowpass) for 30 Hz
B_40, A_40 = butter(6, 40 / TARGET_FS, btype='low', output='ba')

# Notch filter with 50 HZ
F0 = 50.0
Q = 30.0  # Quality factor
# Design notch filter
B_50, A_50 = iirnotch(F0, Q, TARGET_FS)

def parse_command_line_arguments():
    import argparse
    parser = argparse.ArgumentParser(
        description="""Launch server for online decoding.""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # see http://stackoverflow.com/a/24181138/1469195
    parser.add_argument('--savetimestamps', action='store', type=bool,
                        default=False, help='Save the timestamps of every batch')
    args = parser.parse_args()
    return args
class PredictionReceiveServer(gevent.server.StreamServer):
    def __init__(self, listener,
                 handle=None, backlog=None, spawn='default', **ssl_args):
        super(PredictionReceiveServer, self).__init__(listener,
                                                        handle=handle,
                                                        spawn=spawn)
        self.all_preds = []
        self.i_pred_samples = []
        self.connected = False

    def handle(self, socket, address):
        global tcp_recv_socket_preds
        global tcp_recv_preds_connected
        
        print("tcp receiver predictions connected.")
        if self.connected == True:
            print("WARNING: tcp prediction receiver: new incoming connection replaces existing connection!")  
        self.connected = True
        
        tcp_recv_socket_preds = socket
        tcp_recv_preds_connected = True
        
        # keep thread alive to keep socket open
        while True:
            gevent.sleep(365*24*60*60)  # wait one year
        


def connect_lsl_receiver_eeg():
    global lsl_inlet_eeg
    
    print('lsl connect receiver of EEG-Data (from NeurOne)...', end='')
    stream_infos = []
    while len(stream_infos) == 0:
        stream_infos = lsl.resolve_stream('name', LSL_RECEIVER_EEG_NAME)
    if len(stream_infos) > 1:
        print('WARNING: more than one stream from NeurOne found.')
    lsl_inlet_eeg = lsl.StreamInlet(stream_infos[0])
    print('[done]')
            

def connect_lsl_receiver_labels():
    global lsl_inlet_labels
    
    print('lsl connect receiver of labels (from UnityGame)...', end='')
    stream_infos = []
    while len(stream_infos) == 0:
        stream_infos = lsl.resolve_stream('name', LSL_RECEIVER_LABELS_NAME)
    if len(stream_infos) > 1:
        print('WARNING: more than one stream from Unity game found.')
    lsl_inlet_labels = lsl.StreamInlet(stream_infos[0])
    print('[done]')
    
            
def connect_lsl_sender_predictions():
    global lsl_outlet_predictions
    
    print('lsl start StreamOutlet for predictions...', end='')
    stream_info = lsl.StreamInfo(LSL_SENDER_PREDS_NAME, \
                                 LSL_SENDER_PREDS_TYPE, \
                                 LSL_SENDER_PREDS_STREAMCHANNELS, \
                                 lsl.IRREGULAR_RATE, \
                                 'float32', \
                                 LSL_SENDER_PREDS_ID)
    lsl_outlet_predictions = lsl.StreamOutlet(stream_info)
    print('[done]')     
    
            
def start_tcp_receiver_predictions():
    global tcp_recv_server_preds
    
    print("tcp start receiver of predictions...", end='')
    tcp_recv_server_preds = PredictionReceiveServer((TCP_RECEIVER_PREDS_HOSTNAME, \
                                                     TCP_RECEIVER_PREDS_PORT))
    tcp_recv_server_preds.start()
    print("[done]")

    
    
def connect_tcp_sender_eeg(savetimestamps):
    global tcp_send_socket_eeg
    
    print('tcp connect sender of EEG-data (to braindecode-online)...', end='')
    connected_successfully = False
    while not connected_successfully:
        try:
            tcp_send_socket_eeg = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_send_socket_eeg.connect((TCP_SENDER_EEG_HOSTNAME, TCP_SENDER_EEG_PORT))
            connected_successfully = True
        except Exception as e:
            tcp_send_socket_eeg.close()
            # print('.', end='')
            gevent.sleep(2)
    print('[done]')
    
    gevent.sleep(2) # allow other server some time to react to connection first
    print('tcp sender eeg data: send header to braindecode-online...', end='')
    if savetimestamps:
        chan_line = " ".join(EEG_CHANNELNAMES_TIME) + "\n"
    else:
        chan_line = " ".join(EEG_CHANNELNAMES) + "\n"
    tcp_send_socket_eeg.send(chan_line.encode('utf-8'))
    if savetimestamps:
        tcp_send_socket_eeg.send(np.array([TCP_SENDER_EEG_NCHANS_TIME], dtype=np.int32).tobytes())
    else:
        tcp_send_socket_eeg.send(np.array([TCP_SENDER_EEG_NCHANS], dtype=np.int32).tobytes())
    tcp_send_socket_eeg.send(np.array([TCP_SENDER_EEG_NSAMPLES/DOWNSAMPLING_COEF], dtype=np.int32).tobytes())
    print('[done]')
    
def label_gatherer(eeg_samplebuffer, DOWNSAMPLING_COEF, savetimestamps):
    if savetimestamps:
        eeg_labels = (np.array([np.max(eeg_samplebuffer[-2, i:i + DOWNSAMPLING_COEF]) for i in
                                np.arange(0, TCP_SENDER_EEG_NSAMPLES, DOWNSAMPLING_COEF)]).reshape(-1, 1)).astype('float32')
    else:
        eeg_labels = (np.array([np.max(eeg_samplebuffer[-1, i:i + DOWNSAMPLING_COEF]) for i in
                                np.arange(0, TCP_SENDER_EEG_NSAMPLES, DOWNSAMPLING_COEF)]).reshape(-1, 1)).astype(
            'float32')
    return eeg_labels

def filter_and_downsample(eeg_samplebuffer, DOWNSAMPLING_COEF, savetimestamps):
    if savetimestamps:
        eeg_samplebuffer_filt = filtfilt(B_50, A_50, eeg_samplebuffer[:-2, :])
        eeg_samplebuffer_filt = filtfilt(B_40, A_40, eeg_samplebuffer_filt)
        eeg_samplebuffer_filt = filtfilt(B_1, A_1, eeg_samplebuffer_filt)
    else:
        eeg_samplebuffer_filt = filtfilt(B_50, A_50, eeg_samplebuffer[:-1, :])
        eeg_samplebuffer_filt = filtfilt(B_40, A_40, eeg_samplebuffer_filt)
        eeg_samplebuffer_filt = filtfilt(B_1, A_1, eeg_samplebuffer_filt)
    # Resample
    eeg_samples = np.array([np.mean(eeg_samplebuffer_filt[:, i:i + DOWNSAMPLING_COEF], axis=1) for i in
                            np.arange(0, TCP_SENDER_EEG_NSAMPLES, DOWNSAMPLING_COEF)]).astype('float32')
    return eeg_samples

def timestamp_gatherer(eeg_samplebuffer, DOWNSAMPLING_COEF):
    eeg_timestamps = (np.array([np.mean(eeg_samplebuffer[-1, i:i + DOWNSAMPLING_COEF]) for i in
                            np.arange(0, TCP_SENDER_EEG_NSAMPLES, DOWNSAMPLING_COEF)]).reshape(-1, 1)).astype('float32')
    return eeg_timestamps

def concatenate_data_labels_stamps(eeg_samples, eeg_labels, eeg_timestamps=np.array([])):
    if eeg_timestamps.any():
        eeg_samplebuffer_filt_mean = np.concatenate([eeg_samples, eeg_labels,eeg_timestamps ], axis=1).T
    else:
        eeg_samplebuffer_filt_mean = np.concatenate([eeg_samples, eeg_labels], axis=1).T
    return eeg_samplebuffer_filt_mean

class AsyncSocketReader:
    def __init__(self, socket):
        self.line = ''
        self.lines = []
        self.socket = socket
    
    def readlines_string(self, timeout=None, num_lines=1):
        """ Read from socket until newline reached or timeout (in seconds) is over.
            If parameter timeout=None the timeout for readlines_string is socket.gettimeout().
            If timeout is over, TimeoutError is thrown. No bytes are lost, reading can be resumed. """
        starttime = time.time()
        original_timeout = self.socket.gettimeout()
        if timeout == None:
            timeout = original_timeout
        
        while True:
            if len(self.line) == 0:
                pass
            elif self.line[-1] == '\n':
                self.lines.append(self.line)
                self.line = ''
                if len(self.lines) == num_lines:
                    self.socket.settimeout(original_timeout)
                    returnlines = self.lines
                    self.lines = []
                    return returnlines
            
            elapsed_time = time.time() - starttime
            remaining_time = timeout - elapsed_time
            if remaining_time < 0.001:
                self.socket.settimeout(original_timeout)
                raise TimeoutError
            self.socket.settimeout(remaining_time)
            try:
                self.line += self.socket.recv(1).decode('utf-8')
            except socket_timeout:
                pass



    
    
def forward_forever(savetimestamps):
    forwarding_loopcounter = 0
    loop_time = time.time()
    fallen_behind_labels = True
    fallen_behind_eeg = True
    fallen_behind_predictions = True
    tcp_recv_socketreader_preds = AsyncSocketReader(tcp_recv_socket_preds)
    if savetimestamps:
        eeg_samplebuffer = np.zeros((TCP_SENDER_EEG_NCHANS_TIME, 2000), dtype='float32')
    else:
        eeg_samplebuffer = np.zeros((TCP_SENDER_EEG_NCHANS, 2000), dtype='float32')
    eeg_sample_label = 0
    eeg_forwarding_counter = 0
    print('Start forwarding in both directions.')
    # the operations in this loop must not block too long, to ensure that everything stays in sync
    # and we don't fall back behind the data streams
    while True:
        time_elapsed = time.time() - loop_time
        if time_elapsed > 1:
            if fallen_behind_labels == True:
                print('WARNING: falling behind labels, did not wait for them in last 1 sec.')
            if fallen_behind_eeg == True:
                print('WARNING: falling behind eeg, did not wait for them in last 1 sec.')   
            if fallen_behind_predictions == True:
                print('WARNING: falling behind predictions, did not wait for them in last 1 sec.')
            warning_triggered = fallen_behind_labels or fallen_behind_eeg or fallen_behind_predictions
            if warning_triggered:
                print('WARNING: Info:', forwarding_loopcounter, 'loops in', time_elapsed, 'sec.')
            fallen_behind_labels = True
            fallen_behind_eeg = True
            fallen_behind_predictions = True
            loop_time = time.time()
            forwarding_loopcounter = 0
            
        if DEBUG:
            print("")
        forwarding_loopcounter += 1
        
        #
        # read eeg labels from game
        #
        if DEBUG:
            print('read eeg labels from game.')
        eeg_sample_label_unchecked, eeg_timestamp_label = lsl_inlet_labels.pull_sample(timeout=0)
        if eeg_sample_label_unchecked is not None and eeg_timestamp_label is not None:
            eeg_sample_label_unchecked = eeg_sample_label_unchecked[0]
            if eeg_sample_label_unchecked == 'Monster active':
                pass
                # print('new game state:', eeg_sample_label_unchecked)
                # eeg_sample_label = 1
            elif eeg_sample_label_unchecked == 'Monster right':
                print('new game state:', eeg_sample_label_unchecked)
                eeg_sample_label = 1
                predictions = []
            elif eeg_sample_label_unchecked == 'Monster left':
                print('new game state:', eeg_sample_label_unchecked)
                eeg_sample_label = 2
                predictions = []
            elif eeg_sample_label_unchecked == 'Monster destroyed':
                print('new game state:', eeg_sample_label_unchecked)
                eeg_sample_label = 0
            else:
                if DEBUG:
                    print('DEBUG: got some other game state:', eeg_sample_label_unchecked)
        else:
            fallen_behind_labels = False
        
        #
        # read eeg data and forward if there is enough
        #
        if DEBUG:
            print('read eeg data from NeurOne.')
        eeg_samples, eeg_timestamps = lsl_inlet_eeg.pull_chunk(timeout=0.1)
        eeg_samples, eeg_timestamps = np.array(eeg_samples), np.array(eeg_timestamps)
        samples_pulled = eeg_samples.shape[0]
        if eeg_samples.any() is not None and eeg_timestamps.any() is not None:
            if DEBUG:
                print('got new eeg sample.')
            eeg_samples = np.concatenate((eeg_samples[:, :34], eeg_samples[:, 42:-3]), axis=1)
            if savetimestamps:
                eeg_samplebuffer[:, :-samples_pulled] = eeg_samplebuffer[:, samples_pulled:]
                eeg_samplebuffer[:-2,-samples_pulled: ] = eeg_samples.T
                eeg_samplebuffer[-2, -samples_pulled: ] = eeg_sample_label
                eeg_samplebuffer[-1, -samples_pulled:] = eeg_timestamps
            else:
                eeg_samplebuffer[:, :-samples_pulled] = eeg_samplebuffer[:, samples_pulled:]
                eeg_samplebuffer[:-1,-samples_pulled: ] = eeg_samples.T
                eeg_samplebuffer[-1, -samples_pulled: ] = eeg_sample_label
            nonzero_columns = np.nonzero(eeg_samplebuffer)[1]
            first_nonzero = np.nonzero(eeg_samplebuffer)[1][0]
            if (2000-min(nonzero_columns)) > TCP_SENDER_EEG_NSAMPLES :
                for i in range((2000-min(nonzero_columns)) //TCP_SENDER_EEG_NSAMPLES):
                    if DEBUG:
                        print('got enough eeg samples, forwarding. eeg_forwarding_counter:', eeg_forwarding_counter)
                    eeg_forwarding_counter += 1
                    if DEBUG:
                        print('eeg_samplebuffer:', eeg_samplebuffer)
                    #filter the incoming data
                    start = first_nonzero + i * TCP_SENDER_EEG_NSAMPLES
                    datachunk = eeg_samplebuffer[:, start : start + TCP_SENDER_EEG_NSAMPLES ]
                    eeg_labels = label_gatherer(datachunk, DOWNSAMPLING_COEF, savetimestamps)
                    eeg_time_series = filter_and_downsample(datachunk, DOWNSAMPLING_COEF, savetimestamps)
                    if savetimestamps:
                        eeg_timestamps = timestamp_gatherer(datachunk, DOWNSAMPLING_COEF)
                        eeg_samplebuffer_filt_mean = concatenate_data_labels_stamps(eeg_time_series, eeg_labels, eeg_timestamps)
                    else:
                        eeg_samplebuffer_filt_mean = concatenate_data_labels_stamps(eeg_time_series, eeg_labels)
                    tcp_send_socket_eeg.send(eeg_samplebuffer_filt_mean.tobytes(order='F'))
                    if DEBUG:
                        print('forwarding eeg_data done.')
                    eeg_samplebuffer[:, start : start + TCP_SENDER_EEG_NSAMPLES ] = 0
        else:
            fallen_behind_eeg = False
            if DEBUG:
                print('no eeg sample received.')
        # print('forwarding: Instead eeg-data and labels send some random data.')
        # send_random_data(tcp_send_socket_eeg, no_blocks=100)
        # print('forwarding: sending random data done.')
        
        #
        # read predictions and forward them
        # 
        if forwarding_loopcounter % 20 == 0:   # save time, cause readlines_string blocks at least 1ms
            if DEBUG:
                print('read predictions from braindecode-online...')
            try:
                i_sample, preds = tcp_recv_socketreader_preds.readlines_string(timeout=0.001, num_lines=2)
            except TimeoutError:
                fallen_behind_predictions = False
                if DEBUG:
                    print("no prediction to forward yet.")
            else:
                if DEBUG:
                    print('got new prediction.')
                    print('i_sample[:-1] =', i_sample[:-1])  # :-1 => without newline
                    print('preds[:-1] =', preds[:-1])
                    print('forwarding predictions.')
                splitted_predictions = preds.split(" ")
                parsed_predictions = [float(i_sample)] + \
                                    [float(splitted_predictions[i]) for i in range(PREDICTION_NUM_CLASSES)]
                if eeg_sample_label :
                    list_preds = [float(splitted_predictions[0]), float(splitted_predictions[1])]
                    predictions.append(list_preds)
                    all_preds = np.concatenate(predictions).reshape(-1, 2)
                    meaned_preds = np.mean(all_preds, axis=0)
                    max_class = np.argmax(meaned_preds)
                    if meaned_preds[max_class] > 0.05:
                        t, prob = ttest_ind(all_preds[:, max_class], np.ones(all_preds.shape[0])*0.05)
                        if prob < 0.20:
                            action = max_class +1
                            print('Action', action)

                lsl_outlet_predictions.push_sample(np.array(parsed_predictions, dtype='float32'))
                if DEBUG:
                    print('forwarding predictions done.')
    
    
def send_random_data(socket, no_blocks=100):
    print("Send random data...")
    i_block = 0  # if setting i_block to sth higher, printed results will incorrect
    max_stop_block = 10000
    stop_block = no_blocks
    cur_marker = 0
    n_samples_waiting = 49
    n_to_next_marker = n_samples_waiting
    rng = RandomState(3948394)
    assert stop_block < max_stop_block
    while i_block < stop_block:
        # chan x time
        arr = rng.randn(TCP_SENDER_EEG_NCHANS, TCP_SENDER_EEG_NSAMPLES).astype(np.float32)

        arr[-1,:] = cur_marker
        socket.send(arr.tobytes(order='F'))
        assert arr.shape == (TCP_SENDER_EEG_NCHANS, TCP_SENDER_EEG_NSAMPLES)
        i_block += 1
        gevent.sleep(0.03)
        n_to_next_marker -= TCP_SENDER_EEG_NSAMPLES
        if n_to_next_marker <= 0:
            n_to_next_marker = n_samples_waiting
            if cur_marker  == 0:
                cur_marker = float(rng.randint(1,6))
            else:
                cur_marker = 0
    
    
if __name__ == "__main__":
    gevent.signal(signal.SIGINT, gevent.kill)   # used to be signal.SIGQUIT, but didn't work
    args = parse_command_line_arguments()
    connect_lsl_receiver_eeg()
    connect_lsl_receiver_labels()
    connect_lsl_sender_predictions()
    start_tcp_receiver_predictions()
    connect_tcp_sender_eeg(args.savetimestamps)
    
    while tcp_recv_preds_connected == False:
        gevent.sleep(0.01)
    print("Started system successfully.")
    
    forward_forever(args.savetimestamps)
    

    
    
  