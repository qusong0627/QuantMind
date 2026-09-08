# https://github.com/kozistr/pytorch_optimizer
try:
    from pytorch_optimizer import *
except ImportError as e:
    raise RuntimeError(
        "需要安装pytorch_optimizer才能使用此功能: pip install pytorch_optimizer"
    ) from e
from other import base


class Loss(base):
    BCEFocalLoss = BCEFocalLoss
    BCELoss = BCELoss
    BinaryBiTemperedLogisticLoss = BinaryBiTemperedLogisticLoss
    BiTemperedLogisticLoss = BiTemperedLogisticLoss
    DiceLoss = DiceLoss
    FocalCosineLoss = FocalCosineLoss
    FocalLoss = FocalLoss
    FocalTverskyLoss = FocalTverskyLoss
    JaccardLoss = JaccardLoss
    LDAMLoss = LDAMLoss
    LovaszHingeLoss = LovaszHingeLoss
    SoftF1Loss = SoftF1Loss
    TverskyLoss = TverskyLoss


Loss = Loss()


class LrScheduler(base):
    ConstantLR = ConstantLR
    CosineAnnealingLR = CosineAnnealingLR
    CosineAnnealingWarmRestarts = CosineAnnealingWarmRestarts
    CosineAnnealingWarmupRestarts = CosineAnnealingWarmupRestarts
    CosineScheduler = CosineScheduler
    CyclicLR = CyclicLR
    LinearScheduler = LinearScheduler
    MultiplicativeLR = MultiplicativeLR
    MultiStepLR = MultiStepLR
    OneCycleLR = OneCycleLR
    PolyScheduler = PolyScheduler
    ProportionScheduler = ProportionScheduler
    REXScheduler = REXScheduler
    StepLR = StepLR


LrScheduler = LrScheduler()


class Optim(base):
    ADOPT = ADOPT
    APOLLO = APOLLO
    ASGD = ASGD
    BSAM = BSAM
    BCOS = BCOS
    CAME = CAME
    FOCUS = FOCUS
    FTRL = FTRL
    GSAM = GSAM
    LARS = LARS
    LBFGS = LBFGS
    LOMO = LOMO
    MADGRAD = MADGRAD
    MARS = MARS
    MSVAG = MSVAG
    PID = PID
    PNM = PNM
    QHM = QHM
    RACS = RACS
    SAM = SAM
    SCION = SCION
    SGD = SGD
    SGDP = SGDP
    SGDW = SGDW
    SM3 = SM3
    SOAP = SOAP
    SPAM = SPAM
    SRMM = SRMM
    SWATS = SWATS
    TAM = TAM
    TRAC = TRAC
    VSGD = VSGD
    WSAM = WSAM
    A2Grad = A2Grad
    AccSGD = AccSGD
    AdaBelief = AdaBelief
    AdaBound = AdaBound
    AdaDelta = AdaDelta
    AdaFactor = AdaFactor
    AdaGC = AdaGC
    AdaGO = AdaGO
    AdaHessian = AdaHessian
    Adai = Adai
    Conda = Conda
    Adalite = Adalite
    AdaLOMO = AdaLOMO
    Adam = Adam
    AdaMax = AdaMax
    AdamC = AdamC
    AdamG = AdamG
    AdamMini = AdamMini
    AdaMod = AdaMod
    AdamP = AdamP
    AdamS = AdamS
    AdaMuon = AdaMuon
    AdamW = AdamW
    AdamWSN = AdamWSN
    Adan = Adan
    AdaNorm = AdaNorm
    AdaPNM = AdaPNM
    AdaShift = AdaShift
    AdaSmooth = AdaSmooth
    AdaTAM = AdaTAM
    AdEMAMix = AdEMAMix
    AggMo = AggMo
    Aida = Aida
    Alice = Alice
    AliG = AliG
    Amos = Amos
    Ano = Ano
    ApolloDQN = ApolloDQN
    AvaGrad = AvaGrad
    DAdaptAdaGrad = DAdaptAdaGrad
    DAdaptAdam = DAdaptAdam
    DAdaptAdan = DAdaptAdan
    DAdaptLion = DAdaptLion
    DAdaptSGD = DAdaptSGD
    DeMo = DeMo
    DiffGrad = DiffGrad
    DistributedMuon = DistributedMuon
    DynamicLossScaler = DynamicLossScaler
    EmoFact = EmoFact
    EmoLynx = EmoLynx
    EmoNavi = EmoNavi
    EmoNeco = EmoNeco
    EmoZeal = EmoZeal
    EXAdam = EXAdam
    FAdam = FAdam
    Fira = Fira
    FriendlySAM = FriendlySAM
    Fromage = Fromage
    GaLore = GaLore
    Grams = Grams
    Gravity = Gravity
    GrokFastAdamW = GrokFastAdamW
    Kate = Kate
    Kron = Kron
    Lamb = Lamb
    LaProp = LaProp
    Lion = Lion
    Lookahead = Lookahead
    LookSAM = LookSAM
    Muon = Muon
    NAdam = NAdam
    Nero = Nero
    NovoGrad = NovoGrad
    OrthoGrad = OrthoGrad
    PAdam = PAdam
    PCGrad = PCGrad
    Prodigy = Prodigy
    QHAdam = QHAdam
    RAdam = RAdam
    Ranger = Ranger
    Ranger21 = Ranger21
    Ranger25 = Ranger25
    RMSprop = RMSprop
    RotoGrad = RotoGrad
    SafeFP16Optimizer = SafeFP16Optimizer
    ScalableShampoo = ScalableShampoo
    ScheduleFreeAdamW = ScheduleFreeAdamW
    ScheduleFreeRAdam = ScheduleFreeRAdam
    ScheduleFreeSGD = ScheduleFreeSGD
    ScheduleFreeWrapper = ScheduleFreeWrapper
    SCIONLight = SCIONLight
    SGDSaI = SGDSaI
    Shampoo = Shampoo
    SignSGD = SignSGD
    SimplifiedAdEMAMix = SimplifiedAdEMAMix
    SophiaH = SophiaH
    SPlus = SPlus
    StableAdamW = StableAdamW
    StableSPAM = StableSPAM
    Tiger = Tiger
    Yogi = Yogi


Optim = Optim()
