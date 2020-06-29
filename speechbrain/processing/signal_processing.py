"""
Low level signal processing utilities

Author
------
Peter Plantinga 2020
"""
import torch


def compute_amplitude(waveforms, lengths):
    """Compute the average amplitude of a batch of waveforms.

    Arguments
    ---------
    waveform : tensor
        The waveforms used for computing amplitude.
    lengths : tensor
        The lengths of the waveforms excluding the padding
        added to put all waveforms in the same tensor.

    Returns
    -------
    The average amplitude of the waveforms.

    Example
    -------
    >>> signal = torch.sin(torch.arange(16000.0)).unsqueeze(0)
    >>> compute_amplitude(signal, signal.size(1))
    tensor([[0.6366]])
    """
    return torch.sum(input=torch.abs(waveforms), dim=1, keepdim=True,) / lengths


def convolve1d(
    waveform,
    kernel,
    padding=0,
    pad_type="constant",
    stride=1,
    groups=1,
    use_fft=False,
    rotation_index=0,
):
    """Use torch.nn.functional to perform 1d padding and conv.

    Arguments
    ---------
    waveform : tensor
        The tensor to perform operations on.
    kernel : tensor
        The filter to apply during convolution
    padding : int or tuple
        The padding (pad_left, pad_right) to apply.
        If an integer is passed instead, this is passed
        to the conv1d function and pad_type is ignored.
    pad_type : str
        The type of padding to use. Passed directly to
        `torch.nn.functional.pad`, see PyTorch documentation
        for available options.
    stride : int
        The number of units to move each time convolution is applied.
        Passed to conv1d. Has no effect if `use_fft` is True.
    groups : int
        This option is passed to `conv1d` to split the input into groups for
        convolution. Input channels should be divisible by number of groups.
    use_fft : bool
        When `use_fft` is passed `True`, then compute the convolution in the
        spectral domain using complex multiply. This is more efficient on CPU
        when the size of the kernel is large (e.g. reverberation). WARNING:
        Without padding, circular convolution occurs. This makes little
        difference in the case of reverberation, but may make more difference
        with different kernels.
    rotation_index : int
        This option only applies if `use_fft` is true. If so, the kernel is
        rolled by this amount before convolution to shift the output location.

    Returns
    -------
    The convolved waveform.

    Example
    -------
    >>> import soundfile as sf
    >>> signal, rate = sf.read('samples/audio_samples/example1.wav')
    >>> signal = torch.tensor(signal[None, :, None])
    >>> filter = torch.rand(1, 10, 1, dtype=signal.dtype)
    >>> signal = convolve1d(signal, filter, padding=(9, 0))
    """
    if len(waveform.shape) != 3:
        raise ValueError("Convolve1D expects a 3-dimensional tensor")

    # Move time dimension last, which pad and fft and conv expect.
    waveform = waveform.transpose(2, 1)
    kernel = kernel.transpose(2, 1)

    # Padding can be a tuple (left_pad, right_pad) or an int
    if isinstance(padding, tuple):
        waveform = torch.nn.functional.pad(
            input=waveform, pad=padding, mode=pad_type,
        )

    # This approach uses FFT, which is more efficient if the kernel is large
    if use_fft:

        # Pad kernel to same length as signal, ensuring correct alignment
        zero_length = waveform.size(-1) - kernel.size(-1)

        # Handle case where signal is shorter
        if zero_length < 0:
            kernel = kernel[..., :zero_length]
            zero_length = 0

        # Perform rotation to ensure alignment
        zeros = torch.zeros(kernel.size(0), kernel.size(1), zero_length)
        after_index = kernel[..., rotation_index:]
        before_index = kernel[..., :rotation_index]
        kernel = torch.cat((after_index, zeros, before_index), dim=-1)

        # Compute FFT for both signals
        f_signal = torch.rfft(waveform, 1)
        f_kernel = torch.rfft(kernel, 1)

        # Complex multiply
        sig_real, sig_imag = f_signal.unbind(-1)
        ker_real, ker_imag = f_kernel.unbind(-1)
        f_result = torch.stack(
            [
                sig_real * ker_real - sig_imag * ker_imag,
                sig_real * ker_imag + sig_imag * ker_real,
            ],
            dim=-1,
        )

        # Inverse FFT
        convolved = torch.irfft(f_result, 1)

        # Because we're using `onesided`, sometimes the output's length
        # is increased by one in the time dimension. Truncate to ensure
        # that the length is preserved.
        if convolved.size(-1) > waveform.size(-1):
            convolved = convolved[..., : waveform.size(-1)]

    # Use the implemenation given by torch, which should be efficient on GPU
    else:
        convolved = torch.nn.functional.conv1d(
            input=waveform,
            weight=kernel,
            stride=stride,
            groups=groups,
            padding=padding if not isinstance(padding, tuple) else 0,
        )

    # Return time dimension to the second dimension.
    return convolved.transpose(2, 1)


def dB_to_amplitude(SNR):
    """Returns the amplitude ratio, converted from decibels.

    Arguments
    ---------
    SNR : float
        The ratio in decibels to convert.

    Example
    -------
    >>> round(dB_to_amplitude(SNR=10), 3)
    3.162
    >>> dB_to_amplitude(SNR=0)
    1.0
    """
    return 10 ** (SNR / 20)


def notch_filter(notch_freq, filter_width=101, notch_width=0.05):
    """Returns a notch filter constructed from a high-pass and low-pass filter.

    (from https://tomroelandts.com/articles/
    how-to-create-simple-band-pass-and-band-reject-filters)

    Arguments
    ---------
    notch_freq : float
        frequency to put notch as a fraction of the
        sampling rate / 2. The range of possible inputs is 0 to 1.
    filter_width : int
        Filter width in samples. Longer filters have
        smaller transition bands, but are more inefficient.
    notch_width : float
        Width of the notch, as a fraction of the sampling_rate / 2.

    Example
    -------
    >>> import soundfile as sf
    >>> signal, rate = sf.read('samples/audio_samples/example1.wav')
    >>> signal = torch.tensor(signal, dtype=torch.float32)[None, :, None]
    >>> kernel = notch_filter(0.25)
    >>> notched_signal = convolve1d(signal, kernel)
    """

    # Check inputs
    assert 0 < notch_freq <= 1
    assert filter_width % 2 != 0
    pad = filter_width // 2
    inputs = torch.arange(filter_width) - pad

    # Avoid frequencies that are too low
    notch_freq += notch_width

    # Define sinc function, avoiding division by zero
    def sinc(x):
        def _sinc(x):
            return torch.sin(x) / x

        # The zero is at the middle index
        return torch.cat([_sinc(x[:pad]), torch.ones(1), _sinc(x[pad + 1 :])])

    # Compute a low-pass filter with cutoff frequency notch_freq.
    hlpf = sinc(3 * (notch_freq - notch_width) * inputs)
    hlpf *= torch.blackman_window(filter_width)
    hlpf /= torch.sum(hlpf)

    # Compute a high-pass filter with cutoff frequency notch_freq.
    hhpf = sinc(3 * (notch_freq + notch_width) * inputs)
    hhpf *= torch.blackman_window(filter_width)
    hhpf /= -torch.sum(hhpf)
    hhpf[pad] += 1

    # Adding filters creates notch filter
    return (hlpf + hhpf).view(1, -1, 1)


# WORK IN PROGRESS
class EigenH(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, a, b=None):
        """
        Input: (B, 1, K, 2, C+P)
        Output: (B, K, C, C)
        """

        # Extracting data
        batch = a.shape[0]
        n_fft = a.shape[2]
        p = a.shape[4]
        n_channels = int(round(((1 + 8 * p) ** 0.5 - 1) / 2))

        # Converting the input matrices to block matrices
        ash = self.f(a)

        if b is None:
            b = torch.stack(
                (torch.eye(n_channels), torch.zeros((n_channels, n_channels))),
                -1,
            )

            b = torch.stack(n_fft * [b], 0)
            b = torch.stack(batch * [b], 0)

            bsh = self.g(b)

        else:
            bsh = self.f(b)

        # Performing the Cholesky decomposition
        lsh = torch.cholesky(bsh)
        lsh_inv = torch.inverse(lsh)
        lsh_inv_T = torch.transpose(lsh_inv, 2, 3)

        # Computing the matrix C
        csh = torch.matmul(lsh_inv, torch.matmul(ash, lsh_inv_T))

        # Performing the eigenvalue decomposition
        es, ysh = torch.symeig(csh, eigenvectors=True)

        # Collecting the eigenvalues
        dsh = torch.zeros(
            (batch, n_fft, 2 * n_channels, 2 * n_channels),
            dtype=es.dtype,
            device=es.device,
        )

        dsh[..., range(0, 2 * n_channels), range(0, 2 * n_channels)] = es

        # Collecting the eigenvectors
        vsh = torch.matmul(lsh_inv_T, torch.transpose(ysh, 2, 3))

        # Converting the block matrices to full complex matrices
        vs = self.ginv(vsh)
        ds = self.ginv(dsh)

        return vs, ds

    def f(self, ws):
        """
        Input: (B, 1, K, 2, C+P)
        Output: (B, K, 2C, 2C)
        """

        # Formating the input matrix
        ws = ws.transpose(3, 4).squeeze(1)

        # Extracting data
        batch = ws.shape[0]
        n_fft = ws.shape[1]
        p = ws.shape[2]
        n_channels = int(round(((1 + 8 * p) ** 0.5 - 1) / 2))

        # Creating the output matrix
        wsh = torch.zeros(
            (batch, n_fft, 2 * n_channels, 2 * n_channels),
            dtype=ws.dtype,
            device=ws.device,
        )

        # Filling in the output matrix
        indices = torch.triu_indices(n_channels, n_channels)

        wsh[..., indices[1] * 2, indices[0] * 2] = ws[..., 0]
        wsh[..., indices[0] * 2, indices[1] * 2] = ws[..., 0]
        wsh[..., indices[1] * 2 + 1, indices[0] * 2 + 1] = ws[..., 0]
        wsh[..., indices[0] * 2 + 1, indices[1] * 2 + 1] = ws[..., 0]

        wsh[..., indices[0] * 2, indices[1] * 2 + 1] = -1 * ws[..., 1]
        wsh[..., indices[1] * 2 + 1, indices[0] * 2] = -1 * ws[..., 1]
        wsh[..., indices[0] * 2 + 1, indices[1] * 2] = ws[..., 1]
        wsh[..., indices[1] * 2, indices[0] * 2 + 1] = ws[..., 1]

        return wsh

    def finv(self, wsh):
        """
        Input: (B, K, 2C, 2C)
        Output: (B, 1, K, 2, C+P)
        """

        # Extracting data
        batch = wsh.shape[0]
        n_fft = wsh.shape[1]
        n_channels = int(wsh.shape[2] / 2)
        p = int(n_channels * (n_channels + 1) / 2)

        # Output matrix
        ws = torch.zeros(
            (batch, 1, n_fft, 2, p), dtype=wsh.dtype, device=wsh.device
        )

        indices = torch.triu_indices(n_channels, n_channels)

        ws[:, 0, :, 0, :] = wsh[..., indices[0] * 2, indices[1] * 2]
        ws[:, 0, :, 1, :] = -1 * wsh[..., indices[0] * 2, indices[1] * 2 + 1]

        return ws

    def g(self, ws):
        """
        Input: (B, K, C, C, 2)
        Output: (B, K, 2C, 2C)
        """

        # Extracting data
        batch = ws.shape[0]
        n_fft = ws.shape[1]
        n_chan = ws.shape[2]

        # The output matrix
        wsh = torch.zeros(
            (batch, n_fft, 2 * n_chan, 2 * n_chan),
            dtype=ws.dtype,
            device=ws.device,
        )

        wsh[..., slice(0, 2 * n_chan, 2), slice(0, 2 * n_chan, 2)] = ws[..., 0]
        wsh[..., slice(1, 2 * n_chan, 2), slice(1, 2 * n_chan, 2)] = ws[..., 0]
        wsh[..., slice(0, 2 * n_chan, 2), slice(1, 2 * n_chan, 2)] = (
            -1 * ws[..., 1]
        )
        wsh[..., slice(1, 2 * n_chan, 2), slice(0, 2 * n_chan, 2)] = ws[..., 1]

        return wsh

    def ginv(self, wsh):
        """
        Input: (B, K, 2C, 2C)
        Output: (B, K, C, C, 2)
        """

        # Extracting data
        batch = wsh.shape[0]
        n_fft = wsh.shape[1]
        n_chan = int(wsh.shape[2] / 2)

        # Creating the output matrix
        ws = torch.zeros(
            (batch, n_fft, n_chan, n_chan, 2),
            dtype=wsh.dtype,
            device=wsh.device,
        )

        # Output matrix
        ws[..., 0] = wsh[..., slice(0, 2 * n_chan, 2), slice(0, 2 * n_chan, 2)]
        ws[..., 1] = wsh[..., slice(1, 2 * n_chan, 2), slice(0, 2 * n_chan, 2)]

        return ws

    def pos_def(self, ws, alpha=0.001, eps=1e-20):
        """
        Input: (B, 1, K, 2, C+P)
        Output: (B, 1, K, 2, C+P)
        """

        # Extracting data
        p = ws.shape[4]
        n_channels = int(round(((1 + 8 * p) ** 0.5 - 1) / 2))

        # Finding the indices of the diagonal
        indices_triu = torch.triu_indices(n_channels, n_channels)
        indices_diag = torch.eq(indices_triu[0, :], indices_triu[1, :])

        # Computing the trace
        trace = torch.sum(ws[..., 0, indices_diag], 3)
        trace = trace.unsqueeze(3).repeat(1, 1, 1, n_channels)

        # Adding the trace multiplied by alpha to the diagonal
        ws_pf = ws.clone()
        ws_pf[..., 0, indices_diag] += alpha * trace + eps

        return ws_pf
