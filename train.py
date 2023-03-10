import torch
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

import torchvision
import torchvision.transforms as transforms

# from autoaugment import ImageNetPolicy

import argparse
import os
import random
import numpy as np

def set_random_seeds(random_seed=0):

    torch.manual_seed(random_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(random_seed)
    random.seed(random_seed)

def evaluate(model, device, test_loader, epoch, criterion, writer):

    model.eval()

    correct = 0
    total = 0
    avg_loss_test = 0
    with torch.no_grad():
        for data in test_loader:
            images, labels = data[0].to(device), data[1].to(device)
            outputs = model(images)
            _, predicted = torch.max(outputs.data, 1)
            loss = criterion(outputs, labels)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
            avg_loss_test += loss

    avg_loss_test /= len(test_loader.sampler)
    accuracy = correct / total

    if writer:
        writer.add_scalar("Loss/test", avg_loss_test, epoch)
        writer.add_scalar("Accuracy/test", accuracy, epoch)

    return accuracy

def main():

    num_epochs_default = 250 #10000
    batch_size_default = 64 #128 #256 # 1024
    learning_rate_default = 0.1
    random_seed_default = 0
    model_dir_default = "saved_models"
    model_filename_default = "resnet_distributed.pth"

    # Each process runs on 1 GPU device specified by the local_rank argument.
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--local_rank", type=int, help="Local rank. Necessary for using the torch.distributed.launch utility.")
    parser.add_argument("--num_epochs", type=int, help="Number of training epochs.", default=num_epochs_default)
    parser.add_argument("--batch_size", type=int, help="Training batch size for one process.", default=batch_size_default)
    parser.add_argument("--learning_rate", type=float, help="Learning rate.", default=learning_rate_default)
    parser.add_argument("--random_seed", type=int, help="Random seed.", default=random_seed_default)
    parser.add_argument("--model_dir", type=str, help="Directory for saving models.", default=model_dir_default)
    parser.add_argument("--model_filename", type=str, help="Model filename.", default=model_filename_default)
    parser.add_argument("--resume", action="store_true", help="Resume training from saved checkpoint.")
    parser.add_argument("--score", type=str, default="None", help="What type of energy score to use")
    parser.add_argument("--eval", action="store_true", help="Run eval on the model")
    argv = parser.parse_args()

    local_rank = argv.local_rank
    num_epochs = argv.num_epochs
    batch_size = argv.batch_size
    learning_rate = argv.learning_rate
    random_seed = argv.random_seed
    model_dir = argv.model_dir
    model_filename = argv.model_filename
    if (argv.score == "OE"):
        model_filename = model_filename.rsplit('.')[0] + "_OE.pth"
    elif (argv.score == "energy"):
        model_filename = model_filename.rsplit('.')[0] + "_energy.pth"

    resume = argv.resume

    # Create directories outside the PyTorch program
    # Do not create directory here because it is not multiprocess safe
    '''
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    '''

    model_filepath = os.path.join(model_dir, model_filename)

    # We need to use seeds to make sure that the models initialized in different processes are the same
    set_random_seeds(random_seed=random_seed)

    # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
    torch.distributed.init_process_group(backend="nccl")
    # torch.distributed.init_process_group(backend="gloo")

    # Encapsulate the model on the GPU assigned to the current process
    model = torchvision.models.resnet50(pretrained=False)
    model.fc = nn.Sequential( nn.Linear(2048, 101))

    device = torch.device("cuda:{}".format(local_rank))
    model = model.to(device)
    ddp_model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank], output_device=local_rank)

    # We only save the model who uses device "cuda:0"
    # To resume, the device for the saved model would also be "cuda:0"
    if resume == True:
        map_location = {"cuda:0": "cuda:{}".format(local_rank)}
        ddp_model.load_state_dict(torch.load(model_filepath, map_location=map_location))

    # Prepare dataset and dataloader
    # TODO: Define transforms for the training data and testing data
    # imagenet_stats = [(0.485, 0.456, 0.406), (0.229, 0.224, 0.225)]
    train_transforms = transforms.Compose([transforms.RandomRotation(30),
                                        transforms.RandomResizedCrop(224),
                                        transforms.RandomHorizontalFlip(),
                                        transforms.ToTensor(),
                                        transforms.Normalize([0.485, 0.456, 0.406],
                                                                [0.229, 0.224, 0.225])])

    test_transforms = transforms.Compose([transforms.Resize(255),
                                        transforms.CenterCrop(224),
                                        transforms.ToTensor(),
                                        transforms.Normalize([0.485, 0.456, 0.406],
                                                            [0.229, 0.224, 0.225])])

    # Data should be prefetched
    # Download should be set to be False, because it is not multiprocess safe
    train_set = torchvision.datasets.Food101(root="/nobackup/food101", split='train', download=False, transform=train_transforms) 
    test_set = torchvision.datasets.Food101(root="/nobackup/food101", split='test', download=False, transform=test_transforms)

    # Restricts data loading to a subset of the dataset exclusive to the current process
    train_sampler = DistributedSampler(dataset=train_set)

    train_loader = DataLoader(dataset=train_set, batch_size=batch_size, sampler=train_sampler, num_workers=8)
    # Test loader does not have to follow distributed sampling strategy
    test_loader = DataLoader(dataset=test_set, batch_size=128, shuffle=False, num_workers=8)

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(ddp_model.parameters(), lr=learning_rate, momentum=0.9, weight_decay=1e-5)

    writer = SummaryWriter()

    if argv.eval:
        accuracy = evaluate(model=ddp_model, device=device, test_loader=test_loader, epoch=0, criterion=criterion, writer=None)
        print("Accuracy on test data: {}".format(accuracy))
        exit()

    # Loop over the dataset multiple times
    for epoch in range(num_epochs):

        print("Local Rank: {}, Epoch: {}, Training ...".format(local_rank, epoch))
        avg_loss_train = 0
        
        # Save and evaluate model routinely
        if epoch % 10 == 0:
            if local_rank == 0:
                accuracy = evaluate(model=ddp_model, device=device, test_loader=test_loader, epoch=epoch, criterion=criterion, writer=writer)
                torch.save(ddp_model.state_dict(), model_filepath)
                print("-" * 75)
                print("Epoch: {}, Accuracy: {}".format(epoch, accuracy))
                print("-" * 75)

        ddp_model.train()

        for data in train_loader:
            inputs, labels = data[0].to(device), data[1].to(device)
            optimizer.zero_grad()
            outputs = ddp_model(inputs)
            loss = criterion(outputs, labels)


            # https://github.com/wetliu/energy_ood/blob/master/CIFAR/train.py
            if argv.score == "energy":
                Ec_out = -torch.logsumexp(outputs[len(inputs[0]):], dim=1)
                Ec_in = -torch.logsumexp(outputs[:len(inputs[0])], dim=1)
                loss += 0.1*(torch.pow(nn.functional.relu(Ec_in-(-25)), 2).mean() + torch.pow(nn.functional.relu((-7)-Ec_out), 2).mean())
            elif argv.score == "OE":
                loss += 0.5 * -(outputs[len(inputs[0]):].mean(1) - torch.logsumexp(outputs[len(inputs[0]):], dim=1)).mean()

            avg_loss_train += loss
            loss.backward()
            optimizer.step()
        avg_loss_train /= len(train_loader.sampler)
        writer.add_scalar("Loss/train", avg_loss_train, epoch)
    writer.close()

if __name__ == "__main__":
    
    main()
